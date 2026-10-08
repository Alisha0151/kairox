"""Recovery planning, execution and verification (always human-approved)."""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

from .detector import window_stats


def build_plan(rca: dict) -> dict:
    steps = []
    for r in rca.get("recommendations", []):
        if r["action"] == "revert_config":
            steps.append({"action": "revert_config", "service": r["service"], "key": r["key"],
                          "to": r["to"], "reason": r["reason"]})
    return {"steps": steps, "requires_approval": True,
            "note": "" if steps else "No automatic action available; manual investigation required."}


def _post(url: str, body: dict, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def execute(plan: dict, admin_urls: dict[str, str]) -> list[dict]:
    results = []
    for s in plan["steps"]:
        base = admin_urls.get(s["service"])
        if not base:
            results.append({**s, "ok": False, "error": "no admin endpoint registered"})
            continue
        try:
            resp = _post(base.rstrip("/") + "/admin/config", {"key": s["key"], "value": s["to"], "source": "kairox-recovery"})
            results.append({**s, "ok": True, "response": resp})
        except Exception as e:
            results.append({**s, "ok": False, "error": str(e)})
    return results


def verify(store, services: list[str], since: float, until: float, health_check: Callable[[str], bool],
           max_error_rate: float = 0.05, max_p95_ms: float = 400.0, min_requests: int = 3) -> dict:
    """Recovery passes only if error rate, latency AND health-probe signals are all healthy."""
    checks, ok = [], True
    reqs = [e for e in store.events(kind="request", since=since, until=until) if not e["payload"].get("replay")]
    for svc in services:
        st = window_stats(svc, [e for e in reqs if e["service"] == svc])
        healthy = health_check(svc)
        enough = st.count >= min_requests
        passed = enough and st.error_rate <= max_error_rate and st.p95 <= max_p95_ms and healthy
        ok &= passed
        checks.append({"service": svc, "requests": st.count, "error_rate": round(st.error_rate, 3),
                       "p95_ms": round(st.p95, 1), "health_ok": healthy, "enough_traffic": enough, "passed": passed})
    return {"passed": ok, "checks": checks, "checked_at": time.time()}
