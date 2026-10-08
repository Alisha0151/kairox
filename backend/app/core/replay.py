"""Incident replay: re-send the recorded entry-point requests from an incident window.

Replayed calls carry ``x-kairox-replay: 1``. Services must treat that header as
"dry run": no side effects (no stock decrement, no charge) and telemetry tagged
``replay`` so it never feeds back into detection. That is the isolation boundary.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .detector import percentile


def recorded_requests(store, entry_service: str, since: float, until: float, limit: int = 50) -> list[dict]:
    reqs = [e for e in store.events(kind="request", service=entry_service, since=since, until=until)
            if not e["payload"].get("replay") and e["payload"].get("path")]
    return reqs[-limit:]


def _send(base_url: str, rec: dict, timeout: float = 5.0) -> dict:
    p = rec["payload"]
    data = json.dumps(p["body"]).encode() if p.get("body") is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + p["path"], data=data, method=p.get("method", "GET"),
                                 headers={"content-type": "application/json", "x-kairox-replay": "1"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        status = 599
    return {"status": status, "latency_ms": (time.time() - t0) * 1000, "original_status": p.get("status")}


def replay(store, base_url: str, entry_service: str, since: float, until: float,
           limit: int = 30, workers: int = 4) -> dict:
    recs = recorded_requests(store, entry_service, since, until, limit)
    if not recs:
        return {"sent": 0, "error": "no recorded requests in window", "reproduced": False}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda r: _send(base_url, r), recs))
    failed = sum(1 for r in results if r["status"] >= 500)
    orig_failed = sum(1 for r in results if (r["original_status"] or 0) >= 500)
    lat = [r["latency_ms"] for r in results]
    out = {"sent": len(results), "failed": failed, "error_rate": round(failed / len(results), 3),
           "original_error_rate": round(orig_failed / len(results), 3),
           "p95_ms": round(percentile(lat, 0.95), 1)}
    out["reproduced"] = failed > 0 and out["error_rate"] >= 0.5 * out["original_error_rate"]
    return out
