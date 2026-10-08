// Demo API gateway (Node.js, zero dependencies). Entry point of the observed system.
// POST /checkout -> orders -> (inventory, payments). Emits telemetry to Kairox.
"use strict";
const http = require("node:http");
const crypto = require("node:crypto");

const PORT = Number(process.env.PORT || 9100);
const ORDERS_URL = process.env.ORDERS_URL || "http://localhost:9101";
const KAIROX_URL = (process.env.KAIROX_URL || "http://localhost:8000").replace(/\/$/, "");
const config = { request_timeout_ms: 3000 };

const queue = [];
const emit = (kind, payload, traceId, ts) =>
  queue.length < 10000 && queue.push({ kind, service: "gateway", trace_id: traceId || null, ts: ts || Date.now() / 1000, payload });
const emitState = (key, value, source) => emit("state", { key: `config.${key}`, value, source });

setInterval(async () => {
  if (!queue.length) return;
  const events = queue.splice(0, 500);
  try {
    await fetch(`${KAIROX_URL}/ingest`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ events }), signal: AbortSignal.timeout(3000),
    });
  } catch { /* Kairox unreachable: drop telemetry, keep serving */ }
}, 250);

const readJson = (req) => new Promise((resolve) => {
  let raw = "";
  req.on("data", (c) => (raw += c));
  req.on("end", () => { try { resolve(raw ? JSON.parse(raw) : {}); } catch { resolve({}); } });
});
const send = (res, status, obj) => {
  const data = JSON.stringify(obj);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(data) });
  res.end(data);
};

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") return send(res, 200, { status: "ok", service: "gateway" });
  if (req.method === "GET" && req.url === "/admin/config") return send(res, 200, config);
  const body = await readJson(req);
  if (req.method === "POST" && req.url === "/admin/config") {
    const key = String(body.key || "").replace(/^config\./, "");
    if (!(key in config)) return send(res, 400, { error: `unknown config key ${key}` });
    config[key] = body.value;
    emitState(key, body.value, body.source || "admin");
    return send(res, 200, { ok: true, config });
  }
  if (req.method === "POST" && req.url === "/checkout") {
    const t0 = Date.now() / 1000;
    const replay = req.headers["x-kairox-replay"] === "1";
    const traceId = crypto.randomBytes(8).toString("hex");
    const spanId = crypto.randomBytes(6).toString("hex");
    let status = 200, error = null, out;
    try {
      const headers = { "content-type": "application/json", "x-trace-id": traceId, "x-parent-span": spanId };
      if (replay) headers["x-kairox-replay"] = "1";
      const r = await fetch(`${ORDERS_URL}/orders`, {
        method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(config.request_timeout_ms),
      });
      status = r.status;
      out = await r.json().catch(() => ({}));
      if (status >= 500) error = out.error || `orders returned ${status}`;
    } catch (e) {
      status = 504; error = `orders unreachable: ${e.message}`; out = { error };
    }
    const ms = Date.now() - t0 * 1000;
    emit("request", { method: "POST", path: "/checkout", body, status, latency_ms: Math.round(ms * 10) / 10, replay }, traceId, t0);
    emit("span", { span_id: spanId, parent_id: null, name: "POST /checkout", duration_ms: Math.round(ms * 10) / 10,
      status: status >= 500 ? "error" : "ok", error, replay }, traceId, t0);
    if (status >= 500 && !replay) emit("log", { level: "error", message: error }, traceId);
    return send(res, status, out);
  }
  send(res, 404, { error: "not found" });
});

emitState("request_timeout_ms", config.request_timeout_ms, "startup");
server.listen(PORT, "0.0.0.0", () => console.log(`[gateway] listening on :${PORT}`));
