"""Distributed-trace assembly, dependency graph and root-cause ranking."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional


def _span(e: dict) -> dict:
    p = e["payload"]
    return {"trace_id": e["trace_id"], "span_id": p["span_id"], "parent_id": p.get("parent_id"),
            "service": e["service"], "name": p.get("name", ""), "start": e["ts"],
            "duration_ms": float(p.get("duration_ms", 0)), "status": p.get("status", "ok"),
            "error": p.get("error"), "replay": bool(p.get("replay"))}


def build_trace(store, trace_id: str) -> dict:
    spans = [_span(e) for e in store.events(kind="span", trace_id=trace_id)]
    by_id = {s["span_id"]: dict(s, children=[]) for s in spans}
    roots = []
    for s in by_id.values():
        parent = by_id.get(s["parent_id"])
        (parent["children"] if parent else roots).append(s)
    return {"trace_id": trace_id, "spans": spans, "roots": roots,
            "failed": any(s["status"] == "error" for s in spans)}


def dependency_graph(store, since: Optional[float] = None, until: Optional[float] = None) -> dict:
    """caller -> callee edges with call/error counts, derived from parent/child spans."""
    spans = [_span(e) for e in store.events(kind="span", since=since, until=until)]
    by_id = {(s["trace_id"], s["span_id"]): s for s in spans}
    edges: dict[tuple, dict] = defaultdict(lambda: {"calls": 0, "errors": 0})
    for s in spans:
        parent = by_id.get((s["trace_id"], s["parent_id"]))
        if parent and parent["service"] != s["service"]:
            e = edges[(parent["service"], s["service"])]
            e["calls"] += 1
            e["errors"] += s["status"] == "error"
    nodes = sorted({s["service"] for s in spans})
    return {"nodes": nodes,
            "edges": [{"from": a, "to": b, **v} for (a, b), v in sorted(edges.items())]}


def root_cause_candidates(store, since: float, until: float) -> list[dict]:
    """A failing span whose children did not fail is where the error *originated*.

    Errors that merely propagate upward (gateway -> orders -> payments) are therefore
    attributed to the deepest failing service, not the one the user hit.
    """
    spans = [_span(e) for e in store.events(kind="span", since=since, until=until)]
    spans = [s for s in spans if not s["replay"]]
    children = defaultdict(list)
    for s in spans:
        children[(s["trace_id"], s["parent_id"])].append(s)
    origin, affected, examples = Counter(), Counter(), {}
    for s in spans:
        if s["status"] != "error":
            continue
        affected[s["service"]] += 1
        kids = children.get((s["trace_id"], s["span_id"]), [])
        if not any(k["status"] == "error" for k in kids):
            origin[s["service"]] += 1
            examples.setdefault(s["service"], {"trace_id": s["trace_id"], "error": s["error"], "name": s["name"]})
    total = sum(origin.values()) or 1
    return [{"service": svc, "origin_errors": n, "share": round(n / total, 3),
             "affected_spans": affected[svc], "example": examples[svc]}
            for svc, n in origin.most_common()]


def slow_candidates(store, since: float, until: float) -> list[dict]:
    """Self-time (duration minus children) ranks the service that is slow *itself*."""
    spans = [_span(e) for e in store.events(kind="span", since=since, until=until)]
    spans = [s for s in spans if not s["replay"]]
    child_time = defaultdict(float)
    for s in spans:
        child_time[(s["trace_id"], s["parent_id"])] += s["duration_ms"]
    selfs = defaultdict(list)
    for s in spans:
        selfs[s["service"]].append(max(0.0, s["duration_ms"] - child_time.get((s["trace_id"], s["span_id"]), 0.0)))
    rows = [{"service": k, "avg_self_ms": round(sum(v) / len(v), 1), "spans": len(v)} for k, v in selfs.items()]
    return sorted(rows, key=lambda r: r["avg_self_ms"], reverse=True)
