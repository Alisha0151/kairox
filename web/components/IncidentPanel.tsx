"use client";
import type { Incident } from "@/lib/api";

const FLOW = ["detected", "analyzed", "replayed", "proposed", "approved", "verifying", "resolved"] as const;
const LABEL: Record<string, string> = {
  detected: "Detect", analyzed: "Trace & reconstruct", replayed: "Replay", proposed: "Propose fix",
  approved: "Approve", verifying: "Recover", resolved: "Verify",
};

/** The action that moves the incident to its next stage (null when nothing left / terminal). */
function nextAction(status: Incident["status"]): { path: string; label: string; primary?: boolean } | null {
  switch (status) {
    case "detected": return { path: "analyze", label: "Analyze root cause" };
    case "analyzed": return { path: "replay", label: "Replay incident" };
    case "replayed": return { path: "propose", label: "Propose recovery" };
    case "proposed": return { path: "approve", label: "Approve recovery (human)", primary: true };
    case "approved": return { path: "execute", label: "Execute recovery", primary: true };
    case "verifying": return { path: "verify", label: "Verify recovery", primary: true };
    default: return null;
  }
}

interface Props {
  incident: Incident;
  busy: boolean;
  error: string | null;
  onAction: (path: string) => void;
}

export default function IncidentPanel({ incident: inc, busy, error, onAction }: Props) {
  const idx = FLOW.indexOf((inc.status === "recovering" ? "verifying" : inc.status) as (typeof FLOW)[number]);
  const next = nextAction(inc.status);
  const closed = ["resolved", "failed", "dismissed"].includes(inc.status);
  return (
    <div className="card">
      <div className="row between">
        <h2>Incident #{inc.id} <span className={`pill ${inc.status}`}>{inc.status}</span></h2>
        {!closed && <button className="ghost" disabled={busy} onClick={() => onAction("dismiss")}>Dismiss</button>}
      </div>
      <p className="muted">{inc.title}</p>

      <ol className="flow">
        {FLOW.map((s, i) => (
          <li key={s} className={i < idx || inc.status === "resolved" ? "done" : i === idx ? "now" : ""}>{LABEL[s]}</li>
        ))}
      </ol>

      {next && (
        <button className={next.primary ? "primary" : ""} disabled={busy} onClick={() => onAction(next.path)}>
          {busy ? "Working…" : next.label}
        </button>
      )}
      {error && <p className="err">{error}</p>}

      {inc.rca && (
        <section>
          <h3>Root cause <span className="muted">· confidence {(inc.rca.confidence * 100).toFixed(0)}% · {inc.rca.provider}</span></h3>
          <p>{inc.rca.summary}</p>
          {inc.rca.narrative && <blockquote>{inc.rca.narrative}</blockquote>}
          <ul className="evidence">
            {inc.rca.evidence.map((e, i) => (
              <li key={i}><span className={`tag ${e.type}`}>{e.type}</span> {e.text}</li>
            ))}
          </ul>
        </section>
      )}

      {inc.time_travel && inc.time_travel.diff.length > 0 && (
        <section>
          <h3>State diff <span className="muted">· before vs after the failure began</span></h3>
          <table>
            <thead><tr><th>Service</th><th>Key</th><th>Before</th><th>After</th></tr></thead>
            <tbody>
              {inc.time_travel.diff.map((d, i) => (
                <tr key={i}><td>{d.service}</td><td><code>{d.key}</code></td>
                  <td className="ok">{String(d.before)}</td><td className="bad">{String(d.after)}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {inc.replay && (
        <section>
          <h3>Replay <span className="muted">· dry-run, no side effects</span></h3>
          <p>{inc.replay.failed}/{inc.replay.sent} replayed requests failed — failure {inc.replay.reproduced ? "reproduced ✔" : "not reproduced"}.</p>
        </section>
      )}

      {inc.plan && (
        <section>
          <h3>Recovery plan <span className="muted">· requires approval</span></h3>
          {inc.plan.steps.length === 0 && <p className="muted">{inc.plan.note}</p>}
          <ul>{inc.plan.steps.map((s, i) => (
            <li key={i}>Revert <code>{s.service}.{s.key}</code> to <code>{String(s.to)}</code></li>
          ))}</ul>
        </section>
      )}

      {inc.verification && (
        <section>
          <h3>Verification <span className={inc.verification.passed ? "ok" : "bad"}>{inc.verification.passed ? "passed" : "failed"}</span></h3>
          <table>
            <thead><tr><th>Service</th><th>Req</th><th>Errors</th><th>p95</th><th>Health</th></tr></thead>
            <tbody>{inc.verification.checks.map((c) => (
              <tr key={c.service}><td>{c.service}</td><td>{c.requests}</td><td>{(c.error_rate * 100).toFixed(1)}%</td>
                <td>{c.p95_ms.toFixed(0)} ms</td><td className={c.health_ok ? "ok" : "bad"}>{c.health_ok ? "ok" : "down"}</td></tr>
            ))}</tbody>
          </table>
          {inc.verification.replay && <p className="muted">Post-recovery replay: {inc.verification.replay.failed}/{inc.verification.replay.sent} failures.</p>}
        </section>
      )}

      <section>
        <h3>Audit trail</h3>
        <ul className="audit">{inc.timeline.map((t, i) => (
          <li key={i}><time>{new Date(t.ts * 1000).toLocaleTimeString()}</time> <b>{t.stage}</b> {t.note}</li>
        ))}</ul>
      </section>
    </div>
  );
}
