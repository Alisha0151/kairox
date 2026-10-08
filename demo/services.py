"""Demo target system: three small microservices Kairox observes, breaks and repairs.

    gateway (Node) -> orders -> inventory
                            \\-> payments

Run one role per process:  python services.py <orders|inventory|payments> <port>
Uses only the standard library, so it runs anywhere.

Every service
  * exposes /health and /admin/config (GET/POST) so recovery can revert a bad change,
  * emits request + span + log + state telemetry to Kairox (KAIROX_URL/ingest),
  * treats header ``x-kairox-replay: 1`` as a dry run (no side effects, tagged replay).
"""
from __future__ import annotations

import json
import os
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KAIROX_URL = os.environ.get("KAIROX_URL", "http://localhost:8000")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://localhost:9102")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://localhost:9103")

DEFAULTS = {
    "orders": {"downstream_timeout_ms": 2000},
    "inventory": {"index_enabled": True},
    "payments": {"fraud_model_version": "v1", "processor_latency_ms": 20},
}


class Telemetry:
    """Background batching sender; never blocks (or breaks) request handling."""

    def __init__(self, service: str):
        self.service, self.q = service, queue.Queue(maxsize=10000)
        threading.Thread(target=self._loop, daemon=True).start()

    def emit(self, kind: str, payload: dict, trace_id: str | None = None, ts: float | None = None):
        try:
            self.q.put_nowait({"kind": kind, "service": self.service, "trace_id": trace_id,
                               "ts": ts or time.time(), "payload": payload})
        except queue.Full:
            pass

    def _loop(self):
        while True:
            batch = [self.q.get()]
            time.sleep(0.2)
            while not self.q.empty() and len(batch) < 500:
                batch.append(self.q.get_nowait())
            try:
                req = urllib.request.Request(KAIROX_URL.rstrip("/") + "/ingest", json.dumps({"events": batch}).encode(),
                                             {"content-type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass  # Kairox down: drop telemetry, keep serving traffic


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Service:
    def __init__(self, role: str):
        self.role, self.config = role, dict(DEFAULTS[role])
        self.tel = Telemetry(role)
        self.stock = {"sku-1": 1000, "sku-2": 1000, "sku-3": 1000}
        self.lock = threading.Lock()

    def announce_state(self):
        for k, v in self.config.items():
            self.tel.emit("state", {"key": f"config.{k}", "value": v, "source": "startup"})

    def set_config(self, key: str, value, source: str):
        k = key.removeprefix("config.")
        if k not in self.config:
            raise AppError(400, f"unknown config key {k}")
        self.config[k] = value
        self.tel.emit("state", {"key": f"config.{k}", "value": value, "source": source})

    # ---------------------------------------------------------------- business logic
    def call(self, url: str, body: dict, ctx: dict, span_id: str):
        timeout = self.config.get("downstream_timeout_ms", 2000) / 1000
        headers = {"content-type": "application/json", "x-trace-id": ctx["trace_id"], "x-parent-span": span_id}
        if ctx["replay"]:
            headers["x-kairox-replay"] = "1"
        req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise AppError(502, f"downstream {url} returned {e.code}")
        except Exception as e:
            raise AppError(504, f"downstream {url} unreachable: {e}")

    def handle(self, path: str, body: dict, ctx: dict, span_id: str) -> dict:
        if self.role == "orders" and path == "/orders":
            inv = self.call(INVENTORY_URL + "/reserve", {"sku": body.get("sku"), "qty": body.get("qty", 1)}, ctx, span_id)
            pay = self.call(PAYMENTS_URL + "/charge", {"amount": body.get("amount", 0)}, ctx, span_id)
            return {"order_id": uuid.uuid4().hex[:8], "reserved": inv.get("reserved"), "charge": pay.get("charge_id")}
        if self.role == "inventory" and path == "/reserve":
            time.sleep(random.uniform(0.005, 0.02))
            if not self.config["index_enabled"]:
                raise AppError(500, "inventory index disabled: lookup failed")
            sku, qty = body.get("sku", "sku-1"), int(body.get("qty", 1))
            with self.lock:
                if sku not in self.stock or self.stock[sku] < qty:
                    raise AppError(409, "out of stock")
                if not ctx["replay"]:
                    self.stock[sku] -= qty
            return {"reserved": sku, "left": self.stock[sku]}
        if self.role == "payments" and path == "/charge":
            time.sleep(self.config["processor_latency_ms"] / 1000 + random.uniform(0.005, 0.02))
            if self.config["fraud_model_version"] != "v1":
                raise AppError(500, f"fraud model {self.config['fraud_model_version']} failed to load")
            return {"charge_id": uuid.uuid4().hex[:8] if not ctx["replay"] else "dry-run"}
        raise AppError(404, "not found")


def make_handler(svc: Service):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, status: int, obj: dict):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}") if n else {}
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"status": "ok", "service": svc.role})
            if self.path == "/admin/config":
                return self._send(200, svc.config)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            if self.path == "/admin/config":
                try:
                    svc.set_config(body["key"], body["value"], body.get("source", "admin"))
                    return self._send(200, {"ok": True, "config": svc.config})
                except (AppError, KeyError) as e:
                    return self._send(400, {"error": str(e)})
            t0 = time.time()
            ctx = {"trace_id": self.headers.get("x-trace-id") or uuid.uuid4().hex[:16],
                   "replay": self.headers.get("x-kairox-replay") == "1"}
            span_id, parent = uuid.uuid4().hex[:12], self.headers.get("x-parent-span")
            status, err = 200, None
            try:
                out = svc.handle(self.path, body, ctx, span_id)
            except AppError as e:
                status, err, out = e.status, e.message, {"error": e.message}
            ms = (time.time() - t0) * 1000
            failed = status >= 500
            svc.tel.emit("request", {"method": "POST", "path": self.path, "status": status,
                                     "latency_ms": round(ms, 1), "replay": ctx["replay"]}, ctx["trace_id"], t0)
            svc.tel.emit("span", {"span_id": span_id, "parent_id": parent, "name": f"POST {self.path}",
                                  "duration_ms": round(ms, 1), "status": "error" if failed else "ok",
                                  "error": err if failed else None, "replay": ctx["replay"]}, ctx["trace_id"], t0)
            if failed and not ctx["replay"]:
                svc.tel.emit("log", {"level": "error", "message": err}, ctx["trace_id"])
            self._send(status, out)

    return H


def main():
    role, port = sys.argv[1], int(sys.argv[2])
    svc = Service(role)
    svc.announce_state()
    print(f"[{role}] listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc)).serve_forever()


if __name__ == "__main__":
    main()
