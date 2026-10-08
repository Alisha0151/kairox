"""Anomaly detection over request events using a recent window vs. a baseline window."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class WindowStats:
    service: str
    count: int
    error_rate: float
    p50: float
    p95: float

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class Anomaly:
    service: str
    kind: str            # "error_rate" | "latency"
    severity: str        # "warning" | "critical"
    current: WindowStats
    baseline: WindowStats
    started_at: float
    detail: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["current"], d["baseline"] = self.current.to_dict(), self.baseline.to_dict()
        return d


def window_stats(service: str, requests: Iterable[dict]) -> WindowStats:
    reqs = list(requests)
    if not reqs:
        return WindowStats(service, 0, 0.0, 0.0, 0.0)
    lat = [float(r["payload"].get("latency_ms", 0)) for r in reqs]
    errs = sum(1 for r in reqs if int(r["payload"].get("status", 200)) >= 500)
    return WindowStats(service, len(reqs), errs / len(reqs), percentile(lat, 0.5), percentile(lat, 0.95))


class Detector:
    def __init__(self, window_s: float = 10.0, baseline_s: float = 60.0, min_requests: int = 5,
                 error_rate_threshold: float = 0.2, latency_factor: float = 3.0,
                 latency_floor_ms: float = 250.0):
        self.window_s, self.baseline_s, self.min_requests = window_s, baseline_s, min_requests
        self.error_rate_threshold = error_rate_threshold
        self.latency_factor, self.latency_floor_ms = latency_factor, latency_floor_ms

    def evaluate(self, store, now: float) -> list[Anomaly]:
        recent_from = now - self.window_s
        base_from = now - self.window_s - self.baseline_s
        events = [e for e in store.events(kind="request", since=base_from, until=now)
                  if not e["payload"].get("replay")]
        by_service: dict[str, dict[str, list]] = {}
        for e in events:
            bucket = "recent" if e["ts"] >= recent_from else "base"
            by_service.setdefault(e["service"], {"recent": [], "base": []})[bucket].append(e)

        found: list[Anomaly] = []
        for svc, b in by_service.items():
            cur, base = window_stats(svc, b["recent"]), window_stats(svc, b["base"])
            if cur.count < self.min_requests:
                continue
            if cur.error_rate >= self.error_rate_threshold and cur.error_rate > base.error_rate + 0.1:
                sev = "critical" if cur.error_rate >= 0.5 else "warning"
                found.append(Anomaly(svc, "error_rate", sev, cur, base, self._onset(b["recent"], "error"),
                                     f"{svc}: error rate {cur.error_rate:.0%} (baseline {base.error_rate:.0%})"))
            base_p95 = max(base.p95, 1.0)
            if cur.p95 >= self.latency_floor_ms and cur.p95 >= base_p95 * self.latency_factor:
                sev = "critical" if cur.p95 >= base_p95 * self.latency_factor * 2 else "warning"
                slow_after = max(self.latency_floor_ms, base_p95 * self.latency_factor)
                found.append(Anomaly(svc, "latency", sev, cur, base, self._slow_onset(b["recent"], slow_after),
                                     f"{svc}: p95 latency {cur.p95:.0f}ms (baseline {base.p95:.0f}ms)"))
        return found

    @staticmethod
    def _slow_onset(recent: list[dict], threshold_ms: float) -> float:
        """Start of the first request that was actually slow (not just the start of the window)."""
        slow = [e["ts"] for e in recent if float(e["payload"].get("latency_ms", 0)) >= threshold_ms]
        return min(slow) if slow else min(e["ts"] for e in recent)

    @staticmethod
    def _onset(recent: list[dict], what: str) -> float:
        bad = [e["ts"] for e in recent if int(e["payload"].get("status", 200)) >= 500]
        return min(bad) if bad else min(e["ts"] for e in recent)
