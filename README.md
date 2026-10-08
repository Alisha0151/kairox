# Kairox — Causal Time-Travel Debugging & Recovery Platform

Kairox watches a multi-service application, **detects** failures, **traces** them to the service where they
originated, **reconstructs** the system's state at any past moment, **replays** the failing traffic in a
side-effect-free dry run, proposes a **recovery** that a human must approve, and **verifies** the fix using
error-rate, latency, health and replay signals.

```
observe → detect → trace → reconstruct → replay → recover → verify
```

![stack](https://img.shields.io/badge/Next.js-TypeScript-black) ![stack](https://img.shields.io/badge/FastAPI-Python-009688) ![stack](https://img.shields.io/badge/PostgreSQL-Redis-336791) ![stack](https://img.shields.io/badge/Docker-GitHub%20Actions-2496ED)

## Quick start

```bash
cp .env.example .env            # optional: add ANTHROPIC_API_KEY for an LLM-written narrative
docker compose up --build
```

| URL | What |
|---|---|
| http://localhost:3000 | Kairox dashboard |
| http://localhost:8000/docs | API (OpenAPI) |
| http://localhost:9100/checkout | demo gateway (`POST {"sku":"sku-1","qty":1,"amount":20}`) |

### 60-second demo
1. Open the dashboard — four services are green, synthetic traffic is flowing.
2. Click **💥 Bad deploy: payments fraud model v2 fails to load**.
3. Within a few seconds an **incident** appears. Click through the buttons:
   **Analyze root cause** → **Replay incident** → **Propose recovery** → **Approve** → **Execute** → **Verify**.
4. Click any bar in the **Time travel** chart to see the config the system had at that moment.

## What each stage really does

| Stage | Implementation |
|---|---|
| **Observe** | Services push request / span / log / **state-change** events to `POST /ingest` (or standard OTLP/HTTP JSON to `POST /otlp/v1/traces`). Everything is stored as an append-only event log (PostgreSQL; SQLite for local/tests). Live updates over WebSocket (Redis pub/sub mirror). |
| **Detect** | Sliding-window error-rate and p95-latency detector comparing the last N seconds to a baseline; ignores replay traffic and tiny samples. |
| **Trace** | Builds trace trees + a service dependency graph. A failing span whose children are healthy is the *origin*; services that merely propagate the error are not blamed. Latency uses span self-time. |
| **Reconstruct** | Event-sourced time-travel: folding state events up to time *T* gives the exact config at *T*; diff any two instants; correlate changes just before the first failure. |
| **Analyze** | Deterministic, evidence-backed RCA (trace + metric + log + change evidence, confidence, recommendations). If `ANTHROPIC_API_KEY` is set an LLM adds a narrative from *only* that evidence — it cannot change findings or take actions. |
| **Replay** | Re-sends recorded entry requests with `x-kairox-replay: 1`; services treat that as a dry run (no stock decrement, no charge, telemetry tagged and excluded from detection). |
| **Recover** | Plan is derived from time-travel (revert to the value *before* the change). **Requires explicit human approval**; execution is refused before it. |
| **Verify** | Passes only if every service meets error-rate, p95 and health thresholds with enough traffic **and** the original failing requests replay cleanly. |

Failure scenarios: `payments_bad_deploy` (bad config → 500s), `payments_slow` (latency), `inventory_index_off` (misconfiguration).

## Repo layout

```
backend/   FastAPI service + framework-free core (detector, tracer, timetravel, rca, replay, recovery, engine)
demo/      orders / inventory / payments microservices (stdlib Python) with fault + dry-run support
gateway/   Node.js API gateway (entry point of the demo system)
web/       Next.js + TypeScript dashboard
scripts/   loadgen.py, e2e.py (full-loop test against real processes)
docs/      architecture
```

## Develop & test

```bash
# 18 unit tests — no installs needed (stdlib unittest; pytest also works)
python -m unittest discover -s backend/tests -v

# Full loop against real processes: 4 demo services + Kairox API + traffic + fault + recovery (needs Python + Node 20+)
python scripts/e2e.py

# Run the API without Docker
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --port 8000        # or: python -m app.stdlib_server  (zero-dependency)
python demo/services.py inventory 9102 &  python demo/services.py payments 9103 &  python demo/services.py orders 9101 &
node gateway/server.js &  python scripts/loadgen.py
cd web && npm install && npm run dev
```

## Design decisions & honest limits
* The *target* system is a controlled demo; Kairox is not an APM replacement. Recovery supports config reverts only — by design, because they are reversible and verifiable.
* No authentication on the API (it is a local/demo tool). Put it behind auth before exposing it.
* Detection thresholds are simple and tunable (`Detector`). Real fleets would use per-service seasonal baselines.
* Replay isolation relies on services honoring `x-kairox-replay`.
