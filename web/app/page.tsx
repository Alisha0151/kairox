"use client";
import { useCallback, useEffect, useState } from "react";
import IncidentPanel from "@/components/IncidentPanel";
import Timeline, { BUCKET_S } from "@/components/Timeline";
import { API, get, post, type Bucket, type Incident, type Overview, type Scenario, type StateMap } from "@/lib/api";

export default function Dashboard() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [past, setPast] = useState<StateMap | null>(null);
  const [online, setOnline] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const now = Date.now() / 1000;
      const [o, t, i] = await Promise.all([
        get<Overview>("/overview"),
        get<Bucket[]>(`/timeline?since=${now - 150}&until=${now}&bucket=${BUCKET_S}`),
        get<Incident[]>("/incidents"),
      ]);
      setOverview(o); setBuckets(t); setIncidents(i); setOnline(true);
      setSelected((s) => s ?? (i.find((x) => !["resolved", "failed", "dismissed"].includes(x.status)) ?? i[0])?.id ?? null);
    } catch {
      setOnline(false);
    }
  }, []);

  useEffect(() => {
    get<Scenario[]>("/scenarios").then(setScenarios).catch(() => undefined);
    refresh();
    const poll = setInterval(refresh, 1500);
    let ws: WebSocket | null = null;
    try { // optional push channel (FastAPI); polling above is the fallback
      ws = new WebSocket(API.replace(/^http/, "ws") + "/ws");
      ws.onmessage = () => refresh();
    } catch { /* ignore */ }
    return () => { clearInterval(poll); ws?.close(); };
  }, [refresh]);

  // Time travel: reconstruct system state at the picked instant.
  useEffect(() => {
    if (cursor === null) { setPast(null); return; }
    get<{ state: StateMap }>(`/state?at=${cursor}`).then((r) => setPast(r.state)).catch(() => setPast(null));
  }, [cursor]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };

  const incident = incidents.find((i) => i.id === selected) ?? null;

  return (
    <main>
      <header>
        <div>
          <h1>Kairox <span className="muted">causal time-travel debugging &amp; recovery</span></h1>
          <p className="muted">observe → detect → trace → reconstruct → replay → recover → verify</p>
        </div>
        <span className={`pill ${online ? "resolved" : "failed"}`}>{online ? "connected" : "API offline"}</span>
      </header>

      <section className="grid4">
        {overview && Object.entries(overview.services).map(([name, s]) => {
          const bad = !s.health || s.error_rate >= 0.2;
          return (
            <div key={name} className={`card svc ${bad ? "bad-border" : ""}`}>
              <div className="row between"><b>{name}</b><span className={`dot ${s.health ? "up" : "down"}`} /></div>
              <div className="stat">{(s.error_rate * 100).toFixed(0)}<small>% errors</small></div>
              <div className="muted">p95 {s.p95.toFixed(0)} ms · {s.count} req / 30s</div>
            </div>
          );
        })}
      </section>

      <section className="split">
        <div className="card">
          <div className="row between">
            <h2>Time travel</h2>
            {cursor !== null && <button className="ghost" onClick={() => setCursor(null)}>Back to live</button>}
          </div>
          <Timeline buckets={buckets} cursor={cursor} onPick={setCursor} />
          <p className="muted">Click a bar to reconstruct what the system looked like at that moment.</p>
          {past && (
            <table>
              <thead><tr><th>Service</th><th>Config at {new Date((cursor ?? 0) * 1000).toLocaleTimeString()}</th></tr></thead>
              <tbody>{Object.entries(past).map(([svc, kv]) => (
                <tr key={svc}><td>{svc}</td><td>{Object.entries(kv).map(([k, v]) => (
                  <code key={k} className="kv">{k.replace("config.", "")}={String(v)}</code>))}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h2>Controlled failure injection</h2>
          <p className="muted">Break the demo system on purpose, then watch Kairox handle it.</p>
          <div className="col">
            {scenarios.map((s) => (
              <button key={s.id} disabled={busy} onClick={() => run(() => post("/chaos", { scenario: s.id }))}>
                💥 {s.title}
              </button>
            ))}
          </div>
          <h3>Incidents</h3>
          {incidents.length === 0 && <p className="muted">None — all quiet.</p>}
          <ul className="list">
            {incidents.map((i) => (
              <li key={i.id} className={i.id === selected ? "active" : ""} onClick={() => setSelected(i.id)}>
                #{i.id} <span className={`pill ${i.status}`}>{i.status}</span> {i.title}
              </li>
            ))}
          </ul>
        </div>
      </section>

      {incident && (
        <IncidentPanel incident={incident} busy={busy} error={error}
          onAction={(path) => run(() => post(`/incidents/${incident.id}/${path}`, path === "approve" ? { approver: "dashboard-user" } : {}))} />
      )}
    </main>
  );
}
