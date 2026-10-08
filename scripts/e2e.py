"""End-to-end check of the whole Kairox loop against real processes:

  start demo services + Node gateway + Kairox API  ->  steady traffic  ->  inject a fault
  -> detect -> trace/RCA -> time-travel diff -> replay -> propose -> approve -> recover -> verify

Run from repo root:  python scripts/e2e.py        (exit code 0 == every stage passed)
Uses only the Python stdlib + Node; no pip/npm install needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from app.api import Context  # noqa: E402
from app.core.detector import Detector  # noqa: E402
from app.core.store import Store  # noqa: E402
from app.stdlib_server import serve  # noqa: E402
import loadgen  # noqa: E402

KX, GW, OR, INV, PAY = 18000, 19100, 19101, 19102, 19103

# scenario -> (suspect service, config key, value before, value after, produces 5xx errors?)
EXPECT = {
    "payments_bad_deploy": ("payments", "fraud_model_version", "v1", "v2-broken", True),
    "inventory_index_off": ("inventory", "index_enabled", True, False, True),
    "payments_slow": ("payments", "processor_latency_ms", 20, 900, False),
}
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "payments_bad_deploy"
SUSPECT, KEY, BEFORE, AFTER, HARD_ERRORS = EXPECT[SCENARIO]
procs: list[subprocess.Popen] = []
passed = 0


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{KX}{path}", json.dumps(body or {}).encode() if method == "POST" else None,
                                 {"content-type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def check(cond: bool, label: str, detail: str = ""):
    global passed
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not cond:
        cleanup()
        sys.exit(1)
    passed += 1


def spawn(cmd: list[str], env: dict) -> None:
    procs.append(subprocess.Popen(cmd, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def cleanup():
    for p in procs:
        p.terminate()


def wait_for(fn, timeout: float, step: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def main():
    env = {"KAIROX_URL": f"http://127.0.0.1:{KX}", "INVENTORY_URL": f"http://127.0.0.1:{INV}",
           "PAYMENTS_URL": f"http://127.0.0.1:{PAY}", "ORDERS_URL": f"http://127.0.0.1:{OR}"}
    services = {"gateway": f"http://127.0.0.1:{GW}", "orders": f"http://127.0.0.1:{OR}",
                "inventory": f"http://127.0.0.1:{INV}", "payments": f"http://127.0.0.1:{PAY}"}
    ctx = Context(store=Store(":memory:"), services=services)
    ctx.engine.detector = Detector(window_s=4, baseline_s=30, min_requests=5)
    server = serve(ctx, KX)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.start_background(interval=1.0)

    py = sys.executable
    spawn([py, f"{ROOT}/demo/services.py", "inventory", str(INV)], env)
    spawn([py, f"{ROOT}/demo/services.py", "payments", str(PAY)], env)
    spawn([py, f"{ROOT}/demo/services.py", "orders", str(OR)], env)
    spawn(["node", f"{ROOT}/gateway/server.js"], {**env, "PORT": str(GW)})
    health = lambda u: (lambda: urllib.request.urlopen(u + "/health", timeout=1).status == 200)
    ok = all(wait_for(lambda u=u: _try(health(u)), 10) for u in services.values())
    check(ok, "demo services + Node gateway are healthy")

    print("-- steady state", flush=True)
    stop = threading.Event()
    threading.Thread(target=loadgen.run, args=(services["gateway"], 10, stop), daemon=True).start()
    time.sleep(8)
    ov = api("GET", "/overview")
    check(all(s["health"] for s in ov["services"].values()), "overview: every service healthy")
    check(ov["services"]["gateway"]["count"] > 20 and ov["services"]["gateway"]["error_rate"] == 0,
          "baseline traffic observed, 0% errors", f"{ov['services']['gateway']['count']} reqs")
    check(not api("GET", "/incidents"), "no incidents while healthy")
    check(any(e["service"] == "payments" for e in api("GET", "/events/recent?kind=state&limit=20")),
          "startup state recorded as events (time-travel tape)")

    print(f"-- inject fault: {SCENARIO}", flush=True)
    t_fault = time.time()
    check("injected" in api("POST", "/chaos", {"scenario": SCENARIO}), "chaos injected")
    inc = wait_for(lambda: (api("GET", "/incidents") or [None])[0], 20)
    check(bool(inc), "incident detected automatically", f"{time.time() - t_fault:.1f}s after fault")
    iid = inc["id"]

    print("-- trace + reconstruct", flush=True)
    inc = api("POST", f"/incidents/{iid}/analyze")
    r = inc["rca"]
    check(r["suspect_service"] == SUSPECT, "RCA names the originating service (not the gateway that surfaced it)",
          r["suspect_service"])
    check(any(e["type"] == "change" and KEY in e["text"] for e in r["evidence"]),
          "RCA cites the correlated config change as evidence")
    check(any(e["type"] == "trace" for e in r["evidence"]) and (not HARD_ERRORS or any(e["type"] == "log" for e in r["evidence"])),
          "RCA evidence includes trace (+ log for hard errors) items")
    d = inc["time_travel"]["diff"]
    check(any(c["service"] == SUSPECT and c["before"] == BEFORE and c["after"] == AFTER for c in d),
          f"time-travel diff shows {BEFORE} -> {AFTER}", json.dumps(d))

    print("-- replay", flush=True)
    inc = api("POST", f"/incidents/{iid}/replay")
    if HARD_ERRORS:
        check(inc["replay"]["reproduced"], "replay reproduces the failure in dry-run mode",
              f"{inc['replay']['failed']}/{inc['replay']['sent']} failed")
    else:
        check(inc["replay"]["p95_ms"] > 400, "replay reproduces the slowness in dry-run mode", f"p95 {inc['replay']['p95_ms']}ms")

    print("-- recover (human approval gate)", flush=True)
    blocked = api("POST", f"/incidents/{iid}/execute")
    check(blocked.get("_status") == 409, "execute is refused before approval")
    inc = api("POST", f"/incidents/{iid}/propose")
    step = inc["plan"]["steps"][0]
    check(step["action"] == "revert_config" and step["to"] == BEFORE, "proposed plan reverts to the pre-incident value", json.dumps(step))
    api("POST", f"/incidents/{iid}/approve", {"approver": "e2e"})
    inc = api("POST", f"/incidents/{iid}/execute")
    check(inc["status"] == "verifying", "recovery applied, awaiting verification")
    time.sleep(6)  # fresh post-recovery traffic
    inc = api("POST", f"/incidents/{iid}/verify")
    v = inc["verification"]
    check(inc["status"] == "resolved" and v["passed"], "verification passed (error rate, latency, health, clean replay)",
          json.dumps([(c["service"], c["error_rate"]) for c in v["checks"]]))
    check(v["replay"]["failed"] == 0, "post-recovery replay: 0 failures")

    stop.set()
    cleanup()
    print(f"\nALL {passed} CHECKS PASSED", flush=True)


def _try(fn):
    try:
        return fn()
    except Exception:
        return False


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
