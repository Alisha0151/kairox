"""Orchestrates the Kairox workflow:

observe -> detect -> trace -> reconstruct -> replay -> recover -> verify
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable, Optional

from . import recovery, replay as replay_mod, rca, timetravel, tracer
from .detector import Detector
from .store import Store

OPEN = {"detected", "analyzed", "replayed", "proposed", "approved", "recovering", "verifying"}


class Engine:
    def __init__(self, store: Store, services: dict[str, str], entry_service: str = "gateway",
                 replay_url: Optional[str] = None, detector: Optional[Detector] = None,
                 clock: Callable[[], float] = time.time, publish: Optional[Callable[[dict], None]] = None):
        self.store, self.services, self.entry_service = store, services, entry_service
        self.replay_url = replay_url or services.get(entry_service, "")
        self.detector = detector or Detector()
        self.clock = clock
        self.publish = publish or (lambda msg: None)

    # ------------------------------------------------------------------ observe
    def ingest(self, events: list[dict]) -> int:
        n = 0
        for e in events:
            self.store.add_event(e["kind"], e["service"], e.get("payload", {}), ts=e.get("ts"), trace_id=e.get("trace_id"))
            n += 1
        if n:
            self.publish({"type": "events", "count": n})
        return n

    # ------------------------------------------------------------------ detect
    def tick(self) -> dict:
        now = self.clock()
        anomalies = [a.to_dict() for a in self.detector.evaluate(self.store, now)]
        created = None
        if anomalies and not any(i["status"] in OPEN for i in self.store.list_incidents()):
            first = min(anomalies, key=lambda a: a["started_at"])
            data = {"status": "detected", "title": first["detail"], "anomalies": anomalies,
                    "started_at": first["started_at"], "timeline": []}
            data["id"] = self.store.create_incident(data)
            self._log(data, "detected", first["detail"])
            self.store.save_incident(data["id"], data)
            created = data["id"]
            self.publish({"type": "incident", "id": created, "status": "detected"})
        return {"anomalies": anomalies, "created_incident": created}

    # ------------------------------------------------------------------ helpers
    def _log(self, inc: dict, stage: str, note: str) -> None:
        inc.setdefault("timeline", []).append({"ts": self.clock(), "stage": stage, "note": note})

    def _get(self, incident_id: int, allowed: set[str]) -> dict:
        inc = self.store.get_incident(incident_id)
        if not inc:
            raise KeyError(f"incident {incident_id} not found")
        if inc["status"] not in allowed:
            raise ValueError(f"incident is '{inc['status']}', expected one of {sorted(allowed)}")
        return inc

    def _save(self, inc: dict) -> dict:
        self.store.save_incident(inc["id"], inc)
        self.publish({"type": "incident", "id": inc["id"], "status": inc["status"]})
        return self.store.get_incident(inc["id"])

    # ------------------------------------------------------------------ trace + reconstruct
    def analyze(self, incident_id: int) -> dict:
        inc = self._get(incident_id, {"detected", "analyzed"})
        anomaly = min(inc["anomalies"], key=lambda a: a["started_at"])
        # prefer the anomaly on a service that actually failed first in the trace origin ranking
        inc["rca"] = rca.analyze(self.store, anomaly)
        onset = inc["started_at"]
        # "Before" = just prior to the earliest recent change (falls back to 1s pre-onset).
        recent = timetravel.changes_between(self.store, onset - 120.0, onset + 2.0)
        recent = [c for c in recent if c["source"] != "startup"] or recent
        before_at = (min(c["ts"] for c in recent) - 0.001) if recent else onset - 1.0
        after_at = onset + 5.0
        inc["time_travel"] = {
            "before": {"at": before_at, "state": timetravel.reconstruct(self.store, before_at)},
            "after": {"at": after_at, "state": timetravel.reconstruct(self.store, after_at)},
            "diff": timetravel.diff(self.store, before_at, after_at),
        }
        inc["status"] = "analyzed"
        self._log(inc, "analyzed", inc["rca"]["summary"])
        return self._save(inc)

    # ------------------------------------------------------------------ replay
    def replay(self, incident_id: int) -> dict:
        inc = self._get(incident_id, {"analyzed", "replayed"})
        res = replay_mod.replay(self.store, self.replay_url, self.entry_service,
                                inc["started_at"] - 2.0, self.clock())
        inc["replay"] = res
        inc["status"] = "replayed"
        self._log(inc, "replayed", f"{res.get('failed', 0)}/{res.get('sent', 0)} replayed requests failed "
                                   f"(reproduced={res.get('reproduced')})")
        return self._save(inc)

    # ------------------------------------------------------------------ recover
    def propose(self, incident_id: int) -> dict:
        inc = self._get(incident_id, {"analyzed", "replayed", "proposed"})
        inc["plan"] = recovery.build_plan(inc["rca"])
        inc["status"] = "proposed"
        self._log(inc, "proposed", f"{len(inc['plan']['steps'])} recovery step(s) awaiting human approval")
        return self._save(inc)

    def approve(self, incident_id: int, approver: str = "operator") -> dict:
        inc = self._get(incident_id, {"proposed"})
        if not inc["plan"]["steps"]:
            raise ValueError("plan has no executable steps")
        inc["approved_by"], inc["status"] = approver, "approved"
        self._log(inc, "approved", f"approved by {approver}")
        return self._save(inc)

    def execute(self, incident_id: int) -> dict:
        inc = self._get(incident_id, {"approved"})
        inc["status"] = "recovering"
        inc["execution"] = {"started": self.clock(), "results": recovery.execute(inc["plan"], self.services)}
        ok = all(r["ok"] for r in inc["execution"]["results"])
        inc["status"] = "verifying" if ok else "failed"
        self._log(inc, inc["status"], "recovery steps applied" if ok else "a recovery step failed")
        return self._save(inc)

    # ------------------------------------------------------------------ verify
    def _health(self, service: str) -> bool:
        try:
            with urllib.request.urlopen(self.services[service].rstrip("/") + "/health", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def verify(self, incident_id: int, settle_s: float = 1.0) -> dict:
        inc = self._get(incident_id, {"verifying"})
        since = inc["execution"]["started"] + settle_s
        v = recovery.verify(self.store, list(self.services), since, self.clock(), self._health)
        v["replay"] = replay_mod.replay(self.store, self.replay_url, self.entry_service,
                                        inc["started_at"] - 2.0, inc["execution"]["started"])
        v["passed"] = bool(v["passed"] and v["replay"].get("failed", 1) == 0)
        inc["verification"] = v
        inc["status"] = "resolved" if v["passed"] else "failed"
        self._log(inc, inc["status"], "all signals healthy and replay clean" if v["passed"] else "verification failed")
        return self._save(inc)

    def dismiss(self, incident_id: int) -> dict:
        inc = self._get(incident_id, OPEN)
        inc["status"] = "dismissed"
        self._log(inc, "dismissed", "dismissed by operator")
        return self._save(inc)

    # ------------------------------------------------------------------ queries
    def overview(self, window_s: float = 30.0) -> dict:
        from .detector import window_stats
        now = self.clock()
        reqs = [e for e in self.store.events(kind="request", since=now - window_s, until=now)
                if not e["payload"].get("replay")]
        svcs = {}
        for name in self.services:
            st = window_stats(name, [e for e in reqs if e["service"] == name])
            svcs[name] = {**st.to_dict(), "health": self._health(name)}
        return {"now": now, "services": svcs, "graph": tracer.dependency_graph(self.store, now - 120, now)}
