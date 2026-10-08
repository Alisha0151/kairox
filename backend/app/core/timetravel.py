"""Event-sourced state reconstruction ("time-travel").

Services emit ``state`` events: ``{"key": "config.timeout_ms", "value": 800}``.
Folding every state event up to timestamp *T* reproduces what the system looked
like at *T* -- the basis for diffing, change-correlation and safe rollback targets.
"""
from __future__ import annotations

from typing import Optional


def reconstruct(store, at: float, service: Optional[str] = None) -> dict:
    state: dict[str, dict] = {}
    for e in store.events(kind="state", service=service, until=at):
        state.setdefault(e["service"], {})[e["payload"]["key"]] = e["payload"]["value"]
    return state


def diff(store, t1: float, t2: float) -> list[dict]:
    a, b = reconstruct(store, t1), reconstruct(store, t2)
    changes = []
    for svc in sorted(set(a) | set(b)):
        for key in sorted(set(a.get(svc, {})) | set(b.get(svc, {}))):
            va, vb = a.get(svc, {}).get(key), b.get(svc, {}).get(key)
            if va != vb:
                changes.append({"service": svc, "key": key, "before": va, "after": vb})
    return changes


def changes_between(store, start: float, end: float) -> list[dict]:
    """Raw state-change events inside a window, each annotated with the previous value."""
    out = []
    for e in store.events(kind="state", since=start, until=end):
        prev = reconstruct(store, e["ts"] - 1e-6, e["service"]).get(e["service"], {}).get(e["payload"]["key"])
        out.append({"ts": e["ts"], "service": e["service"], "key": e["payload"]["key"],
                    "before": prev, "after": e["payload"]["value"], "source": e["payload"].get("source")})
    return out


def timeline(store, start: float, end: float, bucket_s: float = 5.0) -> list[dict]:
    """Per-bucket request/error/latency series -- feeds the dashboard's time-travel slider."""
    buckets: dict[int, dict] = {}
    for e in store.events(kind="request", since=start, until=end):
        if e["payload"].get("replay"):
            continue
        i = int((e["ts"] - start) // bucket_s)
        b = buckets.setdefault(i, {"requests": 0, "errors": 0, "lat": 0.0})
        b["requests"] += 1
        b["errors"] += int(e["payload"].get("status", 200)) >= 500
        b["lat"] += float(e["payload"].get("latency_ms", 0))
    return [{"t": start + i * bucket_s, "requests": b["requests"], "errors": b["errors"],
             "avg_latency_ms": round(b["lat"] / b["requests"], 1)} for i, b in sorted(buckets.items())]
