"""Evidence-backed root-cause analysis.

The *structured* findings (suspect service, correlated changes, evidence items,
recommendations) are always computed deterministically from logs, traces, metrics
and the state timeline, so every claim is traceable to concrete data.

If ``ANTHROPIC_API_KEY`` is set, an LLM is additionally asked to turn that evidence
into a short narrative for the on-call developer. It only sees the evidence bundle and
can never alter the evidence or trigger actions -- recovery stays human-approved.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

from . import timetravel, tracer


def _recent_logs(store, service: str, since: float, until: float, limit: int = 5) -> list[dict]:
    logs = [e for e in store.events(kind="log", service=service, since=since, until=until)
            if e["payload"].get("level") in ("error", "warn")]
    return [{"ts": e["ts"], "level": e["payload"]["level"], "message": e["payload"].get("message", "")}
            for e in logs[-limit:]]


def analyze(store, anomaly: dict, lookback_s: float = 120.0) -> dict:
    onset = anomaly["started_at"]
    end = onset + 30.0
    start = onset - lookback_s
    evidence: list[dict] = []

    origins = tracer.root_cause_candidates(store, onset - 5.0, end)
    slow = tracer.slow_candidates(store, onset - 5.0, end)
    changes = timetravel.changes_between(store, start, onset + 2.0)
    graph = tracer.dependency_graph(store, start, end)

    # Pick the suspect: error origin for error anomalies, highest self-time for latency.
    suspect: Optional[str] = None
    if anomaly["kind"] == "error_rate" and origins:
        suspect = origins[0]["service"]
        top = origins[0]
        evidence.append({"type": "trace", "service": suspect,
                         "text": f"{top['origin_errors']} failing spans ({top['share']:.0%} of all error origins) "
                                 f"began in '{suspect}'; upstream services only propagated the failure.",
                         "ref": top["example"]})
    elif anomaly["kind"] == "latency" and slow:
        suspect = slow[0]["service"]
        evidence.append({"type": "trace", "service": suspect,
                         "text": f"'{suspect}' has the highest self-time ({slow[0]['avg_self_ms']} ms avg, "
                                 f"excluding downstream calls).", "ref": slow[:3]})
    suspect = suspect or anomaly["service"]

    cur, base = anomaly["current"], anomaly["baseline"]
    evidence.append({"type": "metric", "service": anomaly["service"],
                     "text": f"{anomaly['service']}: error rate {base['error_rate']:.0%} -> {cur['error_rate']:.0%}, "
                             f"p95 {base['p95']:.0f}ms -> {cur['p95']:.0f}ms."})

    logs = _recent_logs(store, suspect, start, end)
    for lg in logs:
        evidence.append({"type": "log", "service": suspect, "text": f"[{lg['level']}] {lg['message']}", "ts": lg["ts"]})

    # Change correlation: a config/state change on the suspect shortly before onset.
    suspect_changes = [c for c in changes if c["service"] == suspect and c["ts"] <= onset + 2.0]
    hypotheses = []
    recs = []
    if suspect_changes:
        c = max(suspect_changes, key=lambda x: x["ts"])
        lead = onset - c["ts"]
        evidence.append({"type": "change", "service": suspect,
                         "text": f"State change on '{suspect}' {lead:.1f}s before the first failure: "
                                 f"{c['key']}: {c['before']!r} -> {c['after']!r}.", "ref": c})
        hypotheses.append({"cause": "bad_change", "service": suspect, "confidence": 0.9 if lead < 60 else 0.6,
                           "description": f"{c['key']} was changed to {c['after']!r} and the failure followed."})
        recs.append({"action": "revert_config", "service": suspect, "key": c["key"],
                     "to": c["before"], "reason": "Revert the change that preceded the failure."})
    else:
        hypotheses.append({"cause": "dependency_or_load", "service": suspect, "confidence": 0.4,
                           "description": f"No recent change found on '{suspect}'; likely a dependency or load problem."})
        recs.append({"action": "investigate", "service": suspect,
                     "reason": "No config change correlates with the failure; inspect dependencies and capacity."})

    blast = sorted({e["to"] for e in graph["edges"] if e["from"] == suspect} |
                   {e["from"] for e in graph["edges"] if e["to"] == suspect})
    confidence = max(h["confidence"] for h in hypotheses)
    cause = hypotheses[0]
    summary = (f"Most likely root cause: '{suspect}' ({cause['cause'].replace('_', ' ')}). "
               f"{cause['description']} Related services: {', '.join(blast) or 'none observed'}.")
    report = {"suspect_service": suspect, "confidence": round(confidence, 2), "summary": summary,
              "hypotheses": hypotheses, "evidence": evidence, "recommendations": recs,
              "related_services": blast, "provider": "rules"}
    narrative = _llm_narrative(report)
    if narrative:
        report["narrative"], report["provider"] = narrative, "rules+llm"
    return report


def _llm_narrative(report: dict) -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        body = json.dumps({
            "model": os.environ.get("KAIROX_LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 400,
            "messages": [{"role": "user", "content":
                "You are an SRE assistant. Using ONLY this evidence JSON, write a 4-sentence incident "
                "summary and the safest next step for the on-call developer. Do not invent facts.\n"
                + json.dumps({k: report[k] for k in ("suspect_service", "evidence", "recommendations")})}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
            "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["content"][0]["text"]
    except Exception:  # never let the optional LLM break incident handling
        return None
