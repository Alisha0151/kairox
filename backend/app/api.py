"""Framework-agnostic API: one route table, served by FastAPI (production) or a
standard-library HTTP server (zero-dependency fallback used by tests and the e2e script)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

from .core import timetravel, tracer
from .core.engine import Engine
from .core.store import Store
from .otlp import convert_traces

DEFAULT_SERVICES = {
    "gateway": "http://localhost:9100", "orders": "http://localhost:9101",
    "inventory": "http://localhost:9102", "payments": "http://localhost:9103",
}

SCENARIOS = {
    "payments_bad_deploy": {"service": "payments", "key": "config.fraud_model_version", "value": "v2-broken",
                            "title": "Bad deploy: payments fraud model v2 fails to load"},
    "payments_slow": {"service": "payments", "key": "config.processor_latency_ms", "value": 900,
                      "title": "Slow dependency: payment processor latency spikes"},
    "inventory_index_off": {"service": "inventory", "key": "config.index_enabled", "value": False,
                            "title": "Misconfiguration: inventory index disabled"},
}


def parse_services() -> dict[str, str]:
    raw = os.environ.get("KAIROX_SERVICES")
    if not raw:
        return dict(DEFAULT_SERVICES)
    return dict(p.split("=", 1) for p in raw.split(",") if "=" in p)


class Bus:
    """Live event fan-out. Local subscribers always work; Redis pub/sub mirroring is optional."""

    def __init__(self, redis_url: Optional[str] = None, channel: str = "kairox:events"):
        self.subs: list[Callable[[dict], None]] = []
        self.channel, self.redis = channel, None
        if redis_url:
            try:
                import redis  # type: ignore
                self.redis = redis.Redis.from_url(redis_url, socket_timeout=1)
                self.redis.ping()
            except Exception:
                self.redis = None

    def publish(self, msg: dict) -> None:
        for fn in list(self.subs):
            try:
                fn(msg)
            except Exception:
                pass
        if self.redis:
            try:
                self.redis.publish(self.channel, json.dumps(msg))
            except Exception:
                pass


class Context:
    def __init__(self, store: Optional[Store] = None, services: Optional[dict] = None,
                 replay_url: Optional[str] = None, bus: Optional[Bus] = None):
        self.store = store or Store(os.environ.get("DATABASE_URL", "kairox.db"))
        self.bus = bus or Bus(os.environ.get("REDIS_URL"))
        self.services = services or parse_services()
        self.engine = Engine(self.store, self.services, "gateway",
                             replay_url or os.environ.get("REPLAY_TARGET_URL"), publish=self.bus.publish)
        self._stop = threading.Event()

    def start_background(self, interval: float = 2.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.engine.tick()
                except Exception as e:  # keep the detector alive
                    print("[kairox] tick error:", e, flush=True)
        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


Response = tuple[int, Any]


def _f(q: dict, key: str, default: Optional[float] = None) -> Optional[float]:
    v = q.get(key)
    return float(v) if v not in (None, "") else default


def _post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


# ---------------------------------------------------------------------- handlers
def health(ctx, p, q, b) -> Response:
    return 200, {"status": "ok", "events": ctx.store.count_events()}


def ingest(ctx, p, q, b) -> Response:
    return 200, {"ingested": ctx.engine.ingest(b.get("events", []))}


def otlp_traces(ctx, p, q, b) -> Response:
    return 200, {"ingested": ctx.engine.ingest(convert_traces(b)), "partialSuccess": {}}


def overview(ctx, p, q, b) -> Response:
    return 200, ctx.engine.overview(_f(q, "window", 30.0))


def recent_events(ctx, p, q, b) -> Response:
    ev = ctx.store.events(kind=q.get("kind"), limit=int(q.get("limit", 50)), newest_first=True)
    return 200, ev


def timeline(ctx, p, q, b) -> Response:
    now = time.time()
    return 200, timetravel.timeline(ctx.store, _f(q, "since", now - 300), _f(q, "until", now), _f(q, "bucket", 5.0))


def state_at(ctx, p, q, b) -> Response:
    at = _f(q, "at", time.time())
    return 200, {"at": at, "state": timetravel.reconstruct(ctx.store, at)}


def trace(ctx, p, q, b) -> Response:
    t = tracer.build_trace(ctx.store, p["id"])
    return (200, t) if t["spans"] else (404, {"error": "trace not found"})


def graph(ctx, p, q, b) -> Response:
    now = time.time()
    return 200, tracer.dependency_graph(ctx.store, _f(q, "since", now - 300), _f(q, "until", now))


def scenarios(ctx, p, q, b) -> Response:
    return 200, [{"id": k, **v} for k, v in SCENARIOS.items()]


def chaos(ctx, p, q, b) -> Response:
    spec = SCENARIOS.get(b.get("scenario", ""))
    if not spec:
        return 400, {"error": "unknown scenario", "available": list(SCENARIOS)}
    base = ctx.services.get(spec["service"])
    try:
        _post_json(base.rstrip("/") + "/admin/config", {"key": spec["key"], "value": spec["value"], "source": "chaos-injector"})
    except Exception as e:
        return 502, {"error": f"could not reach {spec['service']}: {e}"}
    return 200, {"injected": b["scenario"], **spec}


def detect(ctx, p, q, b) -> Response:
    return 200, ctx.engine.tick()


def incidents(ctx, p, q, b) -> Response:
    return 200, ctx.store.list_incidents()


def incident(ctx, p, q, b) -> Response:
    inc = ctx.store.get_incident(int(p["id"]))
    return (200, inc) if inc else (404, {"error": "incident not found"})


def _action(name: str):
    def run(ctx, p, q, b) -> Response:
        fn = getattr(ctx.engine, name)
        try:
            args = (int(p["id"]), b.get("approver", "operator")) if name == "approve" else (int(p["id"]),)
            return 200, fn(*args)
        except KeyError as e:
            return 404, {"error": str(e)}
        except ValueError as e:
            return 409, {"error": str(e)}
    return run


def reset(ctx, p, q, b) -> Response:
    ctx.store.reset()
    return 200, {"ok": True}


ROUTES: list[tuple[str, str, Callable]] = [
    ("GET", "/health", health),
    ("POST", "/ingest", ingest),
    ("POST", "/otlp/v1/traces", otlp_traces),
    ("GET", "/overview", overview),
    ("GET", "/events/recent", recent_events),
    ("GET", "/timeline", timeline),
    ("GET", "/state", state_at),
    ("GET", "/traces/{id}", trace),
    ("GET", "/graph", graph),
    ("GET", "/scenarios", scenarios),
    ("POST", "/chaos", chaos),
    ("POST", "/detect", detect),
    ("GET", "/incidents", incidents),
    ("GET", "/incidents/{id}", incident),
    *[("POST", f"/incidents/{{id}}/{a}", _action(a)) for a in
      ("analyze", "replay", "propose", "approve", "execute", "verify", "dismiss")],
    ("POST", "/reset", reset),
]


def dispatch(ctx: Context, method: str, path: str, query: dict, body: dict) -> Response:
    """Used by the stdlib server; FastAPI registers ROUTES directly."""
    for m, pattern, fn in ROUTES:
        if m != method:
            continue
        rx = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        match = re.match(rx, path)
        if match:
            return fn(ctx, match.groupdict(), query, body)
    return 404, {"error": "not found"}
