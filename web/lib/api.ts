export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface ServiceStat {
  service: string; count: number; error_rate: number; p50: number; p95: number; health: boolean;
}
export interface Overview {
  now: number;
  services: Record<string, ServiceStat>;
  graph: { nodes: string[]; edges: { from: string; to: string; calls: number; errors: number }[] };
}
export interface Bucket { t: number; requests: number; errors: number; avg_latency_ms: number }
export interface Scenario { id: string; service: string; key: string; value: unknown; title: string }
export interface Evidence { type: "trace" | "metric" | "log" | "change"; service: string; text: string }
export interface Step { action: string; service: string; key: string; to: unknown; reason: string }
export interface Check {
  service: string; requests: number; error_rate: number; p95_ms: number; health_ok: boolean; passed: boolean;
}
export interface Incident {
  id: number;
  status: "detected" | "analyzed" | "replayed" | "proposed" | "approved" | "recovering" | "verifying" | "resolved" | "failed" | "dismissed";
  title: string;
  started_at: number;
  timeline: { ts: number; stage: string; note: string }[];
  rca?: { suspect_service: string; confidence: number; summary: string; narrative?: string; provider: string;
          evidence: Evidence[]; related_services: string[] };
  time_travel?: { diff: { service: string; key: string; before: unknown; after: unknown }[] };
  replay?: { sent: number; failed: number; error_rate: number; reproduced: boolean; p95_ms: number };
  plan?: { steps: Step[]; note: string };
  verification?: { passed: boolean; checks: Check[]; replay?: { sent: number; failed: number } };
}
export type StateMap = Record<string, Record<string, unknown>>;

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json() as Promise<T>;
}

export async function post<T>(path: string, body: object = {}): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error ?? `${path}: ${r.status}`);
  return data as T;
}
