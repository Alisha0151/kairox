"use client";
import type { Bucket } from "@/lib/api";

export const BUCKET_S = 3;

interface Props {
  buckets: Bucket[];
  cursor: number | null; // selected time-travel instant (unix seconds) or null = live
  onPick: (t: number | null) => void;
}

/** Request/error bars over time. Clicking a bar "travels" the dashboard to that instant. */
export default function Timeline({ buckets, cursor, onPick }: Props) {
  const W = 760, H = 140, pad = 24;
  if (!buckets.length) return <div className="muted">Waiting for traffic…</div>;
  const max = Math.max(...buckets.map((b) => b.requests), 1);
  const bw = (W - pad * 2) / buckets.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Requests and errors over time">
      {buckets.map((b, i) => {
        const x = pad + i * bw;
        const h = (b.requests / max) * (H - pad * 2);
        const eh = (b.errors / max) * (H - pad * 2);
        const sel = cursor !== null && cursor >= b.t && cursor < b.t + BUCKET_S;
        return (
          <g key={b.t} onClick={() => onPick(b.t)} style={{ cursor: "pointer" }}>
            <rect x={x} y={0} width={bw} height={H} fill="transparent" />
            <rect x={x + 1} y={H - pad - h} width={Math.max(bw - 2, 1)} height={h} fill="var(--ok)" opacity={sel ? 1 : 0.55} />
            <rect x={x + 1} y={H - pad - eh} width={Math.max(bw - 2, 1)} height={eh} fill="var(--bad)" />
            {sel && <rect x={x} y={4} width={bw} height={H - pad} fill="none" stroke="var(--accent)" strokeWidth={2} />}
          </g>
        );
      })}
      <text x={pad} y={H - 6} className="axis">{new Date(buckets[0].t * 1000).toLocaleTimeString()}</text>
      <text x={W - pad} y={H - 6} textAnchor="end" className="axis">
        {new Date(buckets[buckets.length - 1].t * 1000).toLocaleTimeString()}
      </text>
    </svg>
  );
}
