# Architecture

```
                         +-------------------- Kairox ---------------------+
 browser  <--HTTP/WS-->  |  FastAPI  (app.main)  --  same ROUTES --  stdlib |
 (Next.js dashboard)     |        |                                         |
                         |     Engine  --  Detector / Tracer / TimeTravel   |
                         |        |        RCA / Replay / Recovery          |
                         |   Store (PostgreSQL | SQLite)   Bus (+ Redis)    |
                         +----^--------------------------------+------------+
              telemetry       |                                 | admin API (revert config), replay
              POST /ingest    |                                 v
   client --> gateway (Node) --> orders --> inventory      <-- each service: /health, /admin/config
                                      \--> payments
```

## Data model
One append-only table, `events(id, ts, kind, service, trace_id, payload)`:

| kind | payload (key fields) | used by |
|---|---|---|
| `request` | method, path, body, status, latency_ms, replay | detector, replay, verify |
| `span` | span_id, parent_id, duration_ms, status, error | tracer, RCA |
| `log` | level, message | RCA evidence |
| `state` | key, value, source | time-travel, change correlation, rollback target |

`incidents(id, status, data JSON)` stores the workflow, audit trail and every artifact (RCA, diff, replay, plan, verification).

## Incident state machine
`detected → analyzed → replayed → proposed → approved → recovering → verifying → resolved | failed` (`dismissed` from any open state).
Each transition is guarded: calling an action in the wrong state returns HTTP 409, so recovery can never run without an explicit approval.

## Root-cause logic
1. Error anomalies: among failing spans, those with no failing child are *origins*; rank services by origin count.
2. Latency anomalies: rank by average span self-time (duration minus children).
3. Look for a `state` change on the suspect in the 120 s before onset; if found, hypothesise a bad change (confidence 0.9 if < 60 s earlier) and recommend reverting to the previous value. If not found, recommend investigation only — no automated action.

## Replay isolation
Header `x-kairox-replay: 1` propagates gateway → orders → inventory/payments. Services skip side effects and tag telemetry `replay: true`; detection, timeline and verification ignore tagged events.

## Extending
* New fault: add an entry to `SCENARIOS` in `app/api.py` and a config key in `demo/services.py`.
* Real service: send OTLP/HTTP JSON traces to `/otlp/v1/traces` and `state` events (deploys / flag changes) to `/ingest`, and expose `POST /admin/config` for recovery.
