#!/usr/bin/env bun
/**
 * 🌐 Sense SDK browser quickstart — mint an ephemeral client secret.
 *
 *   URUN_API_KEY="<urun-key>" bun examples/mint_server.ts          # http://localhost:8787
 *
 * Your server keeps the org API key and answers exactly one endpoint: mint a
 * 10-minute client secret (POST https://inference.urun.sh/v1/realtime/client_secrets).
 * The browser then dials inference.urun.sh DIRECTLY — the dial route
 * (/v1/realtime/calls) is CORS-open for bearer auth, and the WebTransport/
 * WebRTC media leg connects browser → inference with no relay at all.
 *
 * The browser page (see typescript/README.md → "Browser quickstart"):
 *   const app = new Sense({ apiKey: async () => (await fetch("/client-secret", { method: "POST" })).json().value });
 *   const session = app.session();
 *   const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
 *   const attached = await session.attach(stream);
 *
 * Client secrets are bearer-only (org key never leaves the server), not
 * single-use, and expire in 10 minutes — mint per user session, or hand the
 * browser a minting endpoint it can call again before a redial.
 */
const apiKey = process.env.URUN_API_KEY ?? "";

const usage = `Sense browser quickstart — your browser page does:

  const app = new Sense({ apiKey: async () => (await fetch("/client-secret", { method: "POST" })).json().value });
  const session = app.session();
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
  const attached = await session.attach(stream);

This server answers POST /client-secret with { value: "ek_…", expires_at }.
See typescript/README.md → "Browser quickstart".`;

Bun.serve({
  port: 8787,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname !== "/client-secret" || req.method !== "POST") {
      return new Response(mint_server.usage, { status: url.pathname === "/" ? 200 : 404 });
    }
    if (!apiKey) return new Response("server misconfigured: set URUN_API_KEY", { status: 500 });
    const up = await fetch("https://inference.urun.sh/v1/realtime/client_secrets", {
      method: "POST",
      headers: { authorization: `Bearer ${apiKey}` },
    });
    if (!up.ok) return new Response(`mint failed: ${up.status} ${await up.text()}`, { status: 502 });
    const { value, expires_at } = (await up.json()) as { value: string; expires_at: number };
    return Response.json({ value, expires_at }); // ek_… — the only credential the browser ever sees
  },
});
console.log("🌐 mint server on http://localhost:8787 — GET / for the browser snippet, POST /client-secret to mint");
