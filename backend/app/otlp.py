"""Convert OpenTelemetry OTLP/HTTP JSON trace exports into Kairox span/log events."""
from __future__ import annotations


def _attr(attrs: list[dict], key: str):
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value", {})
            return next(iter(v.values()), None)
    return None


def convert_traces(doc: dict) -> list[dict]:
    events = []
    for rs in doc.get("resourceSpans", []):
        service = _attr(rs.get("resource", {}).get("attributes", []), "service.name") or "unknown"
        for scope in rs.get("scopeSpans", []):
            for sp in scope.get("spans", []):
                start = int(sp.get("startTimeUnixNano", 0))
                end = int(sp.get("endTimeUnixNano", start))
                failed = (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
                msg = (sp.get("status") or {}).get("message")
                events.append({
                    "kind": "span", "service": service, "trace_id": sp["traceId"], "ts": start / 1e9,
                    "payload": {"span_id": sp["spanId"], "parent_id": sp.get("parentSpanId") or None,
                                "name": sp.get("name", ""), "duration_ms": (end - start) / 1e6,
                                "status": "error" if failed else "ok", "error": msg if failed else None}})
                if failed:
                    events.append({"kind": "log", "service": service, "trace_id": sp["traceId"], "ts": end / 1e9,
                                   "payload": {"level": "error", "message": msg or sp.get("name", "span failed")}})
    return events
