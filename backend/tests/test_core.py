"""Unit tests for the Kairox core (stdlib unittest; pytest collects these too)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import rca, recovery, timetravel, tracer  # noqa: E402
from app.core.detector import Detector, percentile  # noqa: E402
from app.core.store import Store  # noqa: E402
from app.otlp import convert_traces  # noqa: E402

T0 = 1_000_000.0


def req(store, svc, ts, status=200, lat=20.0, **kw):
    store.add_event("request", svc, {"status": status, "latency_ms": lat, "path": "/x", **kw}, ts=ts)


def span(store, svc, ts, trace, sid, parent=None, status="ok", dur=10.0, err=None):
    store.add_event("span", svc, {"span_id": sid, "parent_id": parent, "status": status, "duration_ms": dur,
                                  "error": err, "name": "op"}, ts=ts, trace_id=trace)


class StoreTests(unittest.TestCase):
    def test_events_filter_and_order(self):
        s = Store()
        s.add_event("log", "a", {"m": 1}, ts=3)
        s.add_event("log", "b", {"m": 2}, ts=1)
        s.add_event("state", "a", {"key": "k", "value": 1}, ts=2)
        self.assertEqual([e["ts"] for e in s.events()], [1, 2, 3])
        self.assertEqual(len(s.events(kind="log", service="a")), 1)
        self.assertEqual([e["ts"] for e in s.events(since=2, until=3)], [2, 3])
        self.assertEqual(s.events(newest_first=True, limit=1)[0]["ts"], 3)

    def test_incident_roundtrip(self):
        s = Store()
        i = s.create_incident({"status": "detected", "title": "t"})
        s.save_incident(i, {"status": "analyzed", "title": "t"})
        self.assertEqual(s.get_incident(i)["status"], "analyzed")
        self.assertEqual(len(s.list_incidents()), 1)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.d = Detector(window_s=10, baseline_s=60, min_requests=5)

    def test_healthy_traffic_has_no_anomaly(self):
        for i in range(60):
            req(self.s, "api", T0 + i)
        self.assertEqual(self.d.evaluate(self.s, T0 + 60), [])

    def test_error_spike_detected_with_onset(self):
        for i in range(50):
            req(self.s, "api", T0 + i)
        for i in range(50, 60):
            req(self.s, "api", T0 + i, status=500 if i % 2 else 200)
        found = self.d.evaluate(self.s, T0 + 60)
        self.assertEqual([a.kind for a in found], ["error_rate"])
        self.assertEqual(found[0].started_at, T0 + 51)

    def test_latency_spike_detected(self):
        for i in range(50):
            req(self.s, "api", T0 + i, lat=20)
        for i in range(50, 60):
            req(self.s, "api", T0 + i, lat=900)
        self.assertIn("latency", [a.kind for a in self.d.evaluate(self.s, T0 + 60)])

    def test_latency_onset_is_first_slow_request_not_window_start(self):
        for i in range(56):
            req(self.s, "api", T0 + i, lat=20)
        for i in range(56, 60):
            req(self.s, "api", T0 + i, lat=900)
        for i in range(60, 66):
            req(self.s, "api", T0 + i, lat=900)
        a = [x for x in self.d.evaluate(self.s, T0 + 66) if x.kind == "latency"][0]
        self.assertEqual(a.started_at, T0 + 56)

    def test_replay_traffic_is_ignored(self):
        for i in range(10):
            req(self.s, "api", T0 + 55 + i * 0.1, status=500, replay=True)
        self.assertEqual(self.d.evaluate(self.s, T0 + 60), [])

    def test_too_little_traffic_is_ignored(self):
        for i in range(3):
            req(self.s, "api", T0 + 55 + i, status=500)
        self.assertEqual(self.d.evaluate(self.s, T0 + 60), [])

    def test_percentile(self):
        self.assertEqual(percentile([], 0.95), 0.0)
        self.assertAlmostEqual(percentile([1, 2, 3, 4, 5], 0.5), 3)


class TracerTests(unittest.TestCase):
    def _failing_chain(self, s, n=5):
        for i in range(n):
            t = f"t{i}"
            span(s, "gateway", T0 + i, t, "g", None, "error", 100, "orders returned 502")
            span(s, "orders", T0 + i, t, "o", "g", "error", 90, "downstream 502")
            span(s, "payments", T0 + i, t, "p", "o", "error", 80, "fraud model failed")
            span(s, "inventory", T0 + i, t, "i", "o", "ok", 10)

    def test_origin_is_deepest_failing_service(self):
        s = Store()
        self._failing_chain(s)
        cands = tracer.root_cause_candidates(s, T0 - 1, T0 + 10)
        self.assertEqual(cands[0]["service"], "payments")
        self.assertEqual([c["service"] for c in cands], ["payments"])  # gateway/orders only propagate
        self.assertEqual(cands[0]["share"], 1.0)

    def test_trace_tree_and_graph(self):
        s = Store()
        self._failing_chain(s, 2)
        tr = tracer.build_trace(s, "t0")
        self.assertTrue(tr["failed"])
        self.assertEqual(len(tr["roots"]), 1)
        self.assertEqual({c["service"] for c in tr["roots"][0]["children"][0]["children"]}, {"payments", "inventory"})
        g = tracer.dependency_graph(s)
        self.assertIn({"from": "orders", "to": "payments", "calls": 2, "errors": 2}, g["edges"])

    def test_slow_candidates_use_self_time(self):
        s = Store()
        for i in range(5):
            t = f"s{i}"
            span(s, "gateway", T0 + i, t, "g", None, "ok", 1000)
            span(s, "payments", T0 + i, t, "p", "g", "ok", 950)
        self.assertEqual(tracer.slow_candidates(s, T0 - 1, T0 + 10)[0]["service"], "payments")


class TimeTravelTests(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.s.add_event("state", "pay", {"key": "config.v", "value": "v1", "source": "startup"}, ts=T0)
        self.s.add_event("state", "pay", {"key": "config.v", "value": "v2", "source": "deploy"}, ts=T0 + 10)
        self.s.add_event("state", "pay", {"key": "config.v", "value": "v1", "source": "rollback"}, ts=T0 + 20)

    def test_reconstruct_at_points_in_time(self):
        v = lambda t: timetravel.reconstruct(self.s, t)["pay"]["config.v"]
        self.assertEqual((v(T0 + 5), v(T0 + 15), v(T0 + 25)), ("v1", "v2", "v1"))
        self.assertEqual(timetravel.reconstruct(self.s, T0 - 1), {})

    def test_diff_and_changes(self):
        self.assertEqual(timetravel.diff(self.s, T0 + 5, T0 + 15),
                         [{"service": "pay", "key": "config.v", "before": "v1", "after": "v2"}])
        self.assertEqual(timetravel.diff(self.s, T0 + 5, T0 + 25), [])
        ch = timetravel.changes_between(self.s, T0 + 5, T0 + 15)
        self.assertEqual((ch[0]["before"], ch[0]["after"], ch[0]["source"]), ("v1", "v2", "deploy"))


class RcaRecoveryTests(unittest.TestCase):
    def _incident_store(self):
        s = Store()
        s.add_event("state", "payments", {"key": "config.model", "value": "v1", "source": "startup"}, ts=T0 - 100)
        s.add_event("state", "payments", {"key": "config.model", "value": "bad", "source": "deploy"}, ts=T0 - 3)
        for i in range(8):
            t = f"t{i}"
            span(s, "gateway", T0 + i * 0.2, t, "g", None, "error", 50, "502")
            span(s, "payments", T0 + i * 0.2, t, "p", "g", "error", 40, "model failed to load")
            s.add_event("log", "payments", {"level": "error", "message": "model failed to load"}, ts=T0 + i * 0.2)
        return s

    def _anomaly(self):
        z = {"service": "gateway", "count": 8, "error_rate": 1.0, "p50": 10, "p95": 50}
        return {"service": "gateway", "kind": "error_rate", "started_at": T0,
                "current": z, "baseline": {**z, "error_rate": 0.0}, "detail": "x", "severity": "critical"}

    def test_rca_blames_origin_and_cites_change(self):
        r = rca.analyze(self._incident_store(), self._anomaly())
        self.assertEqual(r["suspect_service"], "payments")
        self.assertEqual({e["type"] for e in r["evidence"]}, {"trace", "metric", "log", "change"})
        self.assertEqual(r["recommendations"][0], {"action": "revert_config", "service": "payments", "key": "config.model",
                                                   "to": "v1", "reason": r["recommendations"][0]["reason"]})
        self.assertEqual(r["provider"], "rules")

    def test_rca_without_change_does_not_claim_a_fix(self):
        s = Store()
        for i in range(6):
            span(s, "db", T0 + i, f"t{i}", "d", None, "error", 5, "conn refused")
        a = self._anomaly()
        a["service"] = "db"
        r = rca.analyze(s, a)
        self.assertEqual(r["recommendations"][0]["action"], "investigate")
        self.assertEqual(recovery.build_plan(r)["steps"], [])

    def test_verify_requires_all_signals(self):
        s = Store()
        for i in range(10):
            req(s, "a", T0 + i, lat=20)
        ok = recovery.verify(s, ["a"], T0, T0 + 20, lambda svc: True)
        self.assertTrue(ok["passed"])
        self.assertFalse(recovery.verify(s, ["a"], T0, T0 + 20, lambda svc: False)["passed"])        # unhealthy
        self.assertFalse(recovery.verify(s, ["a"], T0 + 50, T0 + 60, lambda svc: True)["passed"])      # no traffic
        for i in range(10):
            req(s, "b", T0 + i, status=500)
        self.assertFalse(recovery.verify(s, ["a", "b"], T0, T0 + 20, lambda svc: True)["passed"])      # errors


class OtlpTests(unittest.TestCase):
    def test_convert(self):
        doc = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "cart"}}]},
               "scopeSpans": [{"spans": [{"traceId": "ab", "spanId": "01", "name": "GET /", "startTimeUnixNano": "2000000000",
                                          "endTimeUnixNano": "2050000000", "status": {"code": 2, "message": "boom"}}]}]}]}
        ev = convert_traces(doc)
        self.assertEqual([e["kind"] for e in ev], ["span", "log"])
        self.assertEqual(ev[0]["service"], "cart")
        self.assertEqual(ev[0]["payload"]["status"], "error")
        self.assertAlmostEqual(ev[0]["payload"]["duration_ms"], 50.0)


if __name__ == "__main__":
    unittest.main()
