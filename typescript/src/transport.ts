/** Transport layer: one standing bidirectional event wire per session.
 *
 * Every transport carries the SAME OpenAI JSON events (media rides the event
 * channel as `input_audio_buffer.append` / `conversation.item.create`), so the
 * session loop is transport-agnostic:
 *
 * - `quic`   — WebTransport: POST /v1/realtime/calls?transport=quic →
 *              {callId, url, token, certHashes}, then a WT session with a
 *              msgpack hello control stream and an NDJSON events stream.
 *              Native `globalThis.WebTransport` (browsers; Node only if the
 *              global exists).
 * - `webrtc` — RTCPeerConnection: POST the SDP offer, events on the
 *              `oai-events` data channel (byte-for-byte the WS protocol).
 *              Browsers only; Node falls through to WS.
 * - `ws`     — the original websocket wire (Node `ws`, auth via header).
 *
 * `auto` tries QUIC (short dial: UDP may be blocked) → WebRTC → WS.
 */
import { encode as msgpackEncode, decode as msgpackDecode } from '@msgpack/msgpack'
import { errMessage, OutOfCreditsError } from './errors.ts'

export type Transport = 'auto' | 'quic' | 'webrtc' | 'ws'

export const TRANSPORT_DIAL_TIMEOUT_MS: Record<'quic' | 'webrtc', number> = {
  quic: 5_000, // UDP blocked ⇒ QUIC never completes; still fast. 2 s was tighter than
  // the whole POST→WT-ready→hello-ack dance through the console's same-origin
  // relay (1.2–1.8 s direct, 2026-11-15; the relay hop adds its own fetch),
  // so prod fell back off a rung that would have connected.
  webrtc: 30_000, // the calls POST queues through the pool like the WS lane
  // (observed up to 17 s at prod lax 2026-11-18) + ICE gather + DTLS +
  // datachannel open — 10 s expired with the channel milliseconds away
}

/** Largest single inbound websocket message accepted (server events are small JSON). */
export const MAX_INBOUND_MESSAGE_BYTES = 4 * 1024 * 1024
/** Largest partial (newline-less) line buffered from a stream. */
export const MAX_LINE_BYTES = 256 * 1024

/** UTF-8 byte length of a string, computed only when it could matter
 * (`length` counts UTF-16 units; one unit is at most 3 UTF-8 bytes). */
export function utf8Exceeds(text: string, limit: number): boolean {
  if (text.length * 3 <= limit) return false;
  return new TextEncoder().encode(text).byteLength > limit;
}

function isLoopback(host: string): boolean {
  const h = host.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '')
  return h === 'localhost' || h.endsWith('.localhost') || h === '::1' || /^127(\.\d{1,3}){3}$/.test(h)
}

/** Validate a service base URL. The API key rides every dial as a bearer
 * header, so plaintext http/ws is refused unless the host is loopback (local
 * test servers) or `allowInsecure` is set explicitly. */
function parseServiceUrl(url: string): URL {
  const bad = () => new TypeError(`baseUrl must be an absolute https:// or wss:// URL, got ${JSON.stringify(url)}`)
  let u: URL
  try {
    u = new URL(url)
  } catch {
    throw bad()
  }
  if (!['https:', 'wss:', 'http:', 'ws:'].includes(u.protocol) || !u.hostname) throw bad()
  return u
}

export function checkBaseUrl(url: string, allowInsecure = false): string {
  const u = parseServiceUrl(url)
  const scheme = u.protocol.replace(/:$/, '')
  if (u.username || u.password) throw new TypeError('baseUrl must not embed credentials; pass apiKey instead')
  if ((scheme === 'http' || scheme === 'ws') && !allowInsecure && !isLoopback(u.hostname)) {
    throw new TypeError(
      `refusing plaintext baseUrl ${JSON.stringify(url)}: the API key would be sent unencrypted. ` +
        'Use https:// or wss://, or pass allowInsecure: true for a trusted network.',
    )
  }
  return url
}

/** A non-WS transport could not be established (fallback signal in auto). */
export class TransportDialError extends Error {}

/** The WS dial was refused with 503/429/502 — worth a backoff redial (§7).
 * 502 is the hub realtime relay's "member SFU refused the upgrade" — transient
 * while a game server is being re-allocated. */
export class DialRefused extends Error {
  constructor(readonly status: number) {
    super(`dial refused: ${status}`)
  }
}

// ---------------------------------------------------------------------------
// platform seams — minimal structural types (no DOM lib in this package)
// ---------------------------------------------------------------------------

interface WtReader {
  read(): Promise<{ value: Uint8Array | undefined; done: boolean }>
}

interface WtWriter {
  write(chunk: Uint8Array): Promise<void>
}

interface WtBidirectionalStream {
  readable: { getReader(): WtReader }
  writable: { getWriter(): WtWriter }
}

interface WtSession {
  ready: Promise<void>
  closed: Promise<void>
  createBidirectionalStream(): Promise<WtBidirectionalStream>
  close(): void
}

/** `new WebTransport(url, { serverCertificateHashes })` (WHATWG WebTransport). */
type WtCtor = new (
  url: string,
  options: { serverCertificateHashes: Array<{ algorithm: 'sha-256'; value: Uint8Array }> },
) => WtSession

interface RtcDataChannel {
  readyState: 'open' | 'connecting' | 'closing' | 'closed'
  send(data: string): void
  close(): void
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: (() => void) | null
  onopen: (() => void) | null
}

interface RtcPeerConnection {
  connectionState: 'new' | 'connecting' | 'connected' | 'disconnected' | 'failed' | 'closed'
  iceConnectionState: string
  localDescription: { sdp: string } | null
  createDataChannel(label: string): RtcDataChannel
  addTransceiver(kind: 'audio' | 'video', init: { direction: 'sendonly' | 'recvonly' | 'sendrecv' | 'inactive' }): unknown
  createOffer(): Promise<{ type: 'offer'; sdp: string }>
  setLocalDescription(desc: { type: string; sdp: string }): Promise<void>
  setRemoteDescription(desc: { type: string; sdp: string }): Promise<void>
  close(): void
  onconnectionstatechange: (() => void) | null
}

/** `new RTCPeerConnection(config)` (W3C WebRTC). */
type RtcCtor = new (config: { iceServers: never[] }) => RtcPeerConnection

/** Node `ws` emitter surface (plus the browser-WebSocket addEventListener). */
interface WsLike {
  send(data: string): void
  close(): void
  addEventListener?(type: string, cb: () => void, opts?: { once?: boolean }): void
  on?(event: string, cb: (...args: never[]) => void): unknown
  once?(event: string, cb: (...args: never[]) => void): unknown
  off?(event: string, cb: (...args: never[]) => void): unknown
  removeAllListeners?(): unknown
}

/** `new WebSocket(url, { headers, maxPayload, followRedirects })` from the `ws` package. */
type WsCtor = new (
  url: string,
  options: { headers: Record<string, string>; maxPayload: number; followRedirects: false },
) => WsLike

/** Node ships no WebTransport. When the global is missing on Node, load the
 * maintained ponyfill @fails-components/webtransport (WHATWG-shaped client
 * over HTTP/3; the native quiche binding ships prebuilt, no compile on
 * install — live-verified against prod lax 2026-11-18). Browsers have a
 * native and never touch this path; the runtime-built specifier keeps
 * browser bundlers from resolving the import into their bundles. WebRTC
 * stays ws-only on Node: werift 0.27.0 completes the SDP exchange but its
 * ICE/DTLS never opens the datachannel against the lax SFU (phase-logged
 * 30 s, 2026-11-18), and no prebuilt W3C-shaped alternative was validated. */
async function nodePonyfill(): Promise<unknown | undefined> {
  const env = globalThis as typeof globalThis & { process?: { versions?: { node?: string } } }
  if (typeof env.process?.versions?.node !== 'string') return undefined
  try {
    const pkg = '@fails' + '-components/webtransport'
    const mod = (await import(pkg)) as { WebTransport?: unknown }
    // The HTTP/3 binding (@fails-components/webtransport-transport-http3-quiche)
    // is OPTIONAL: it carries a compile-on-install lifecycle script, so the SDK
    // never ships it as a dependency (a missing binding must degrade to ws,
    // never break install). The base package's own `quicheLoaded` promise
    // RESOLVES undefined on a missing binding (its internal catch swallows the
    // import failure) and would silently fall back to WebTransport-over-HTTP/2,
    // which the hub does not serve — probe the binding with our own import and
    // refuse the rung when it is absent. Apps that want Node QUIC install it.
    const binding = await import('@fails' + '-components/webtransport-transport-http3-quiche')
      .then(() => true, () => false)
    if (!binding) return undefined
    return mod.WebTransport ?? undefined
  } catch {
    return undefined // ponyfill not loadable on this platform: caller falls back
  }
}

function isNodeRuntime(): boolean {
  const env = globalThis as typeof globalThis & { process?: { versions?: { node?: string } } }
  return typeof env.process?.versions?.node === 'string'
}

function globalCtor(name: string): unknown {
  // Cross-runtime constructor probe: this package's tsconfig has no DOM/node
  // lib, so globalThis is not typed for WebTransport/RTCPeerConnection.
  const g = globalThis as typeof globalThis & Record<string, unknown>
  const value = g[name]
  return typeof value === 'function' ? value : undefined
}

// ---------------------------------------------------------------------------
// wire contract
// ---------------------------------------------------------------------------

export interface Wire {
  readonly kind: Transport
  /** Fire-and-forget send of one JSON event. Throws once the wire is closed. */
  sendJson(obj: unknown): void
  onMessage(cb: (raw: string) => void): void
  onClose(cb: (code?: number, reason?: string) => void): void
  close(): Promise<void>
}

export interface WireOptions {
  transport: Transport
  /** https:// base of the inference proxy (POST /v1/realtime/calls). */
  apiBaseUrl: string
  apiKey: string
  model: string
  /** Client-chosen sticky id the calls edge requires (?dcid): reconnects
   * with the same id re-bind to the same member ("first caller binds"). */
  dcid: string
  /** wss://…/v1/realtime?model=… (the WS dial URL). */
  wsUrl: string
}

export type WireDialer = (opts: WireOptions) => Promise<Wire>

// ---------------------------------------------------------------------------
// shared helpers
// ---------------------------------------------------------------------------

export async function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  const { promise, reject } = Promise.withResolvers<never>()
  const timer = setTimeout(() => reject(new TransportDialError(`${label} timed out after ${ms}ms`)), ms)
  try {
    return await Promise.race([p, promise])
  } finally {
    clearTimeout(timer)
  }
}

/** certHashes entries: the gateway serves base64 sha-256; tolerate lowercase hex. */
export function hashToBytes(h: string): Uint8Array {
  if (h.length === 64 && /^[0-9a-fA-F]+$/.test(h)) {
    const out = new Uint8Array(32)
    for (let i = 0; i < 32; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16)
    return out
  }
  let bin: string
  try {
    bin = atob(h)
  } catch {
    throw new TransportDialError(`certHashes entry is neither base64 nor hex sha-256: ${h}`)
  }
  if (bin.length !== 32) throw new TransportDialError(`certHashes entry is neither base64 nor hex sha-256: ${h}`)
  const out = new Uint8Array(32)
  for (let i = 0; i < 32; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** Incremental NDJSON splitter: feed bytes, get complete lines. A partial
 * line longer than `maxLineBytes` is discarded and sets `overflowed` instead
 * of growing without bound; complete lines from the same chunk are still
 * returned. */
export class NdjsonSplitter {
  private buf = ''
  overflowed = false

  constructor(private readonly maxLineBytes = MAX_LINE_BYTES) {}

  push(text: string): string[] {
    this.buf += text
    const out: string[] = []
    for (;;) {
      const i = this.buf.indexOf('\n')
      if (i < 0) break
      const line = this.buf.slice(0, i)
      this.buf = this.buf.slice(i + 1)
      if (line.trim()) out.push(line)
    }
    if (utf8Exceeds(this.buf, this.maxLineBytes)) {
      this.buf = ''
      this.overflowed = true
    }
    return out
  }
}

// ---------------------------------------------------------------------------
// WT control stream: [u32 LE length][msgpack body] — gateway's canonical dance
// ---------------------------------------------------------------------------

type ControlMessage = { t: string; ts?: number; id?: number; token?: string; sessionId?: string }

const WT_CONTROL_FRAME_MAX = 256 * 1024

function encodeWtControlFrame(msg: ControlMessage): Uint8Array {
  const body = msgpackEncode(msg)
  const framed = new Uint8Array(4 + body.byteLength)
  new DataView(framed.buffer).setUint32(0, body.byteLength, true)
  framed.set(body, 4)
  return framed
}

class WtControlDecoder {
  private buf = new Uint8Array(0)

  push(bytes: Uint8Array): ControlMessage[] {
    const merged = new Uint8Array(this.buf.byteLength + bytes.byteLength)
    merged.set(this.buf)
    merged.set(bytes, this.buf.byteLength)
    this.buf = merged
    const out: ControlMessage[] = []
    while (this.buf.byteLength >= 4) {
      const len = new DataView(this.buf.buffer, this.buf.byteOffset).getUint32(0, true)
      if (len > WT_CONTROL_FRAME_MAX) throw new TransportDialError('wt control frame too large')
      if (this.buf.byteLength < 4 + len) break
      const body = this.buf.subarray(4, 4 + len)
      this.buf = this.buf.subarray(4 + len)
      out.push(msgpackDecode(body) as ControlMessage)
    }
    return out
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

/** The close frame's reason string (Node `ws` delivers it as bytes). */
function closeReason(reason: unknown): string | undefined {
  try {
    if (typeof reason === 'string') return reason
    if (reason instanceof Uint8Array) return new TextDecoder().decode(reason)
  } catch {
    // a hostile reason must not break close handling
  }
  return undefined
}

export class WsWire implements Wire {
  readonly kind: Transport = 'ws'
  private messageCb: ((raw: string) => void) | null = null
  private closeCb: ((code?: number, reason?: string) => void) | null = null

  constructor(private ws: WsLike) {
    ws.on?.('message', (data: unknown) => this.messageCb?.(String(data)))
    ws.on?.('close', (code: number, reason: unknown) => this.closeCb?.(code, closeReason(reason)))
    // EventEmitter: an unhandled 'error' throws (crashes the host process);
    // 'close' follows and reports the death through closeCb.
    ws.on?.('error', () => undefined)
  }

  sendJson(obj: unknown): void {
    this.ws.send(JSON.stringify(obj))
  }

  onMessage(cb: (raw: string) => void): void {
    this.messageCb = cb
  }

  onClose(cb: (code?: number, reason?: string) => void): void {
    this.closeCb = cb
  }

  close(): Promise<void> {
    const { promise, resolve } = Promise.withResolvers<void>()
    this.ws.once?.('close', () => resolve())
    this.ws.close()
    return promise
  }
}

/** Dial the WS wire. Node uses the `ws` package (Authorization header
 * support); a browser global WebSocket cannot set headers, so browser WS
 * fallback is only viable where the proxy accepts URL-carried auth.
 *
 * Dynamic import: `ws` is a Node-only package — a static import would break
 * every browser bundle of this SDK (the platform-specific-module exception). */
export const dialWs: WireDialer = async (opts) => {
  const Impl = await webSocketImpl()
  const { promise, resolve, reject } = Promise.withResolvers<WsWire>()
  const ws = new Impl(opts.wsUrl, {
    headers: { Authorization: `Bearer ${opts.apiKey}` },
    maxPayload: MAX_INBOUND_MESSAGE_BYTES,
    followRedirects: false,
  })
  const fail = (err: unknown) => {
    ws.removeAllListeners?.()
    // Keep a no-op 'error' listener: some runtimes (Bun's ws) emit 'error'
    // again after the first, and an EventEmitter 'error' with no listener
    // throws — one refused dial would otherwise crash the host process.
    ws.on?.('error', () => undefined)
    reject(err)
  }
  ws.once?.('unexpected-response', (_req: unknown, res: { statusCode?: number }) => {
    const status = res.statusCode ?? 0
    fail(
      status === 402
        ? new OutOfCreditsError() // admission gate: terminal, never retried
        : status === 503 || status === 429 || status === 502
          ? new DialRefused(status)
          : new TransportDialError(`handshake refused: ${status}`),
    )
  })
  ws.once?.('error', fail)
  const onOpen = () => {
    ws.off?.('error', fail)
    resolve(new WsWire(ws))
  }
  if (typeof ws.once === 'function') ws.once('open', onOpen)
  else ws.addEventListener?.('open', onOpen, { once: true })
  return promise
}

async function webSocketImpl(): Promise<WsCtor> {
  // Cross-runtime Node probe: globalThis.process is untyped (no node lib here).
  const env = globalThis as typeof globalThis & { process?: { versions?: { node?: string } } }
  const isNode = typeof env.process?.versions?.node === 'string'
  if (isNode) {
    const { default: WS } = await import('ws')
    return WS as unknown as WsCtor // `ws`'s typed ctor accepts the structural WsLike surface
  }
  const ctor = globalCtor('WebSocket') as WsCtor | undefined
  if (ctor === undefined) throw new TransportDialError('no WebSocket implementation on this platform')
  // Browser WebSocket cannot set the Authorization header ({headers} becomes
  // the protocols arg → SyntaxError) and has no EventEmitter surface — dialWs
  // needs both. Fail clearly instead of a fallback that can never work.
  throw new TransportDialError('browser WebSocket fallback is unsupported (no header auth)')
}

// ---------------------------------------------------------------------------
// QUIC / WebTransport
// ---------------------------------------------------------------------------

interface CallsAnswer {
  callId: string
  url: string
  token: string
  certHashes: string[]
}

/** Validate the /calls answer. An empty certHashes list fails closed: the
 * edge serves a self-signed cert, so without pins the session would rely on
 * platform behavior instead of an explicit authentication decision. */
export function parseCallsAnswer(raw: unknown): CallsAnswer {
  const a = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  for (const k of ['callId', 'url', 'token'] as const) {
    if (typeof a[k] !== 'string' || a[k] === '') throw new TransportDialError(`calls answer is missing ${k}`)
  }
  const hashes = a.certHashes
  if (!Array.isArray(hashes) || hashes.length === 0 || !hashes.every((h) => typeof h === 'string')) {
    throw new TransportDialError('no certHashes served; refusing unauthenticated QUIC')
  }
  return { callId: a.callId as string, url: a.url as string, token: a.token as string, certHashes: hashes as string[] }
}

export class QuicWire implements Wire {
  readonly kind: Transport = 'quic'
  private messageCb: ((raw: string) => void) | null = null
  private closeCb: ((code?: number, reason?: string) => void) | null = null
  private writer: WtWriter | null = null
  private splitter = new NdjsonSplitter()
  private wt: WtSession | null = null
  private closedSent = false

  static available(): boolean {
    // Node has no native WebTransport but the @fails-components ponyfill is a
    // regular dependency; a failed ponyfill load falls back at dial time.
    return globalCtor('WebTransport') !== undefined || isNodeRuntime()
  }

  constructor(private opts: WireOptions) {}

  async connect(): Promise<void> {
    const WT = (globalCtor('WebTransport') as WtCtor | undefined) ??
      ((await nodePonyfill()) as WtCtor | undefined)
    if (WT === undefined) throw new TransportDialError('no WebTransport implementation on this platform')

    const res = await fetch(
      `${this.opts.apiBaseUrl.replace(/\/+$/, '')}/v1/realtime/calls?model=${encodeURIComponent(this.opts.model)}&transport=quic&dcid=${encodeURIComponent(this.opts.dcid)}`,
      // Never replay the bearer key to wherever a redirect points.
      { method: 'POST', headers: { authorization: `Bearer ${this.opts.apiKey}` }, redirect: 'error' },
    )
    if (res.status === 402) throw new OutOfCreditsError() // admission gate: terminal, never retried
    if (res.status !== 201) {
      throw new TransportDialError(`POST /v1/realtime/calls?transport=quic -> ${res.status}: ${(await res.text()).slice(0, 200)}`)
    }
    const answer = parseCallsAnswer(await res.json())

    const wt = new WT(answer.url, {
      serverCertificateHashes: answer.certHashes.map((h) => ({ algorithm: 'sha-256' as const, value: hashToBytes(h) })),
    })
    this.wt = wt
    await wt.ready

    // canonical control dance: msgpack hello {token, sessionId} → hello-ack
    const ctrl = await wt.createBidirectionalStream()
    const ctrlWriter = ctrl.writable.getWriter()
    await ctrlWriter.write(encodeWtControlFrame({ t: 'hello', token: answer.token, sessionId: answer.callId }))
    const helloAcked = Promise.withResolvers<void>()
    const pumpControl = async (): Promise<void> => {
      const reader = ctrl.readable.getReader()
      const decoder = new WtControlDecoder()
      for (;;) {
        const { value, done } = await reader.read()
        if (done) throw new TransportDialError('gateway closed the control stream before hello-ack')
        for (const msg of decoder.push(value!)) {
          if (msg.t === 'hello-ack') {
            helloAcked.resolve()
          } else if (msg.t === 'ping') {
            // u64 ping ids ride through as Numbers (exact < 2^53);
            // byte-mirror the msgpack body if a gateway ever checks wider ids.
            await ctrlWriter.write(encodeWtControlFrame({ t: 'pong', ts: msg.ts ?? 0, id: msg.id ?? 0 }))
          }
        }
      }
    }
    pumpControl().catch((err: unknown) => {
      helloAcked.reject(err)
      this.signalClosed()
    })
    await helloAcked.promise

    // events stream: NDJSON, both directions, exact OpenAI JSON
    const events = await wt.createBidirectionalStream()
    this.writer = events.writable.getWriter()
    const pumpEvents = async (): Promise<void> => {
      const reader = events.readable.getReader()
      const decoder = new TextDecoder()
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        for (const line of this.splitter.push(decoder.decode(value, { stream: true }))) {
          this.messageCb?.(line)
        }
        if (this.splitter.overflowed) {
          throw new TransportDialError(`events line exceeds ${MAX_LINE_BYTES} bytes`)
        }
      }
    }
    pumpEvents()
      .catch((err: unknown) => {
        // read side died — report closed below
        if (err instanceof TransportDialError) console.error(`[sense] quic events stream rejected: ${err.message}`)
      })
      // Close the session itself, not just report it: the control pump and
      // transport must not outlive a rejected events stream.
      .finally(() => void this.close())
    wt.closed.then(() => this.signalClosed()).catch(() => this.signalClosed())
  }

  private signalClosed(): void {
    if (this.closedSent) return
    this.closedSent = true
    this.closeCb?.()
  }

  sendJson(obj: unknown): void {
    if (this.writer === null || this.closedSent) throw new TransportDialError('quic wire closed')
    // Writes are not awaited, so this send path does not apply backpressure.
    this.writer.write(new TextEncoder().encode(JSON.stringify(obj) + '\n')).catch(() => this.signalClosed())
  }

  onMessage(cb: (raw: string) => void): void {
    this.messageCb = cb
  }

  onClose(cb: (code?: number) => void): void {
    this.closeCb = cb
  }

  async close(): Promise<void> {
    try {
      this.wt?.close()
    } catch {
      // already closed
    }
    this.signalClosed()
  }
}

// ---------------------------------------------------------------------------
// WebRTC
// ---------------------------------------------------------------------------

export class RtcWire implements Wire {
  readonly kind: Transport = 'webrtc'
  private messageCb: ((raw: string) => void) | null = null
  private closeCb: ((code?: number, reason?: string) => void) | null = null
  private pc: RtcPeerConnection | null = null
  private dc: RtcDataChannel | null = null

  static available(): boolean {
    // Browser-only today: Node has no maintained W3C-shaped WebRTC that
    // interoperates with the lax SFU without compiling on install
    // (werift evidence above), so Node keeps ws for this rung.
    return globalCtor('RTCPeerConnection') !== undefined
  }

  constructor(private opts: WireOptions) {}

  async connect(): Promise<void> {
    const RTCP = globalCtor('RTCPeerConnection') as RtcCtor | undefined
    if (RTCP === undefined) throw new TransportDialError('no RTCPeerConnection on this platform')

    // No STUN/TURN: the SFU is reached via host candidates on its announced ports.
    const pc = new RTCP({ iceServers: [] })
    this.pc = pc
    const dc = pc.createDataChannel('oai-events')
    this.dc = dc
    // The SDK's media rides the event channel as JSON; the audio m-line exists
    // so the SFU's sendrecv merge can answer. It must be sendrecv: the media
    // edge rejects an offer without a=ssrc lines, and browsers emit none for a
    // recvonly m-line. No track is attached, so no RTP is ever sent.
    pc.addTransceiver('audio', { direction: 'sendrecv' })

    dc.onmessage = (ev) => {
      const data = ev.data
      const tooBig =
        typeof data === 'string'
          ? utf8Exceeds(data, MAX_INBOUND_MESSAGE_BYTES)
          : (data as ArrayBuffer).byteLength > MAX_INBOUND_MESSAGE_BYTES
      if (tooBig) {
        // Same bound as the WS leg: end the wire rather than parse it.
        console.error(`[sense] webrtc message exceeds ${MAX_INBOUND_MESSAGE_BYTES} bytes; closing the wire`)
        void this.close()
        return
      }
      this.messageCb?.(typeof data === 'string' ? data : new TextDecoder().decode(data as ArrayBuffer))
    }
    dc.onclose = () => this.closeCb?.()
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed') this.closeCb?.()
    }

    await pc.setLocalDescription(await pc.createOffer())
    const res = await fetch(
      `${this.opts.apiBaseUrl.replace(/\/+$/, '')}/v1/realtime/calls?model=${encodeURIComponent(this.opts.model)}&dcid=${encodeURIComponent(this.opts.dcid)}`,
      {
        method: 'POST',
        headers: { authorization: `Bearer ${this.opts.apiKey}`, 'content-type': 'application/sdp' },
        body: pc.localDescription!.sdp,
        redirect: 'error',
      },
    )
    if (res.status === 402) throw new OutOfCreditsError() // admission gate: terminal, never retried
    if (res.status !== 201) {
      throw new TransportDialError(`POST /v1/realtime/calls -> ${res.status}: ${(await res.text()).slice(0, 200)}`)
    }
    await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() })
    await this.waitDcOpen()
  }

  private async waitDcOpen(): Promise<void> {
    const { promise, resolve, reject } = Promise.withResolvers<void>()
    const dc = this.dc!
    const pc = this.pc!
    if (dc.readyState === 'open') return
    const poll = setInterval(() => {
      if (pc.connectionState === 'failed' || pc.iceConnectionState === 'failed') {
        clearInterval(poll)
        reject(new TransportDialError('peer connection failed before datachannel open'))
      }
    }, 100)
    dc.onopen = () => {
      clearInterval(poll)
      resolve()
    }
    await promise
  }

  sendJson(obj: unknown): void {
    if (this.dc === null || this.dc.readyState !== 'open') throw new TransportDialError('webrtc wire closed')
    this.dc.send(JSON.stringify(obj))
  }

  onMessage(cb: (raw: string) => void): void {
    this.messageCb = cb
  }

  onClose(cb: (code?: number) => void): void {
    this.closeCb = cb
  }

  async close(): Promise<void> {
    try {
      this.dc?.close()
    } catch {
      // already closed
    }
    try {
      this.pc?.close()
    } catch {
      // already closed
    }
    this.closeCb?.()
  }
}

// ---------------------------------------------------------------------------
// selection
// ---------------------------------------------------------------------------

/** Dial order. Auto = quic → webrtc → ws, skipping platforms without the
 * client stack. Explicit overrides dial exactly one. */
export function transportOrder(transport: Transport): Array<'quic' | 'webrtc' | 'ws'> {
  if (transport !== 'auto') return [transport]
  const order: Array<'quic' | 'webrtc' | 'ws'> = []
  if (QuicWire.available()) order.push('quic')
  if (RtcWire.available()) order.push('webrtc')
  order.push('ws')
  return order
}

export const defaultDialers: Record<'quic' | 'webrtc' | 'ws', WireDialer> = {
  ws: dialWs,
  quic: async (opts) => {
    const wire = new QuicWire(opts)
    try {
      await wire.connect()
    } catch (err) {
      await wire.close()
      throw err
    }
    return wire
  },
  webrtc: async (opts) => {
    const wire = new RtcWire(opts)
    try {
      await wire.connect()
    } catch (err) {
      await wire.close()
      throw err
    }
    return wire
  },
}

export async function connectWire(
  opts: WireOptions,
  dialers: Partial<Record<'quic' | 'webrtc' | 'ws', WireDialer>> = {},
): Promise<Wire> {
  const dial = (name: 'quic' | 'webrtc' | 'ws'): Promise<Wire> =>
    dialers[name] ? dialers[name]!(opts) : defaultDialers[name](opts)
  for (const name of transportOrder(opts.transport)) {
    if (name === 'ws') {
      console.info('[sense] transport=ws')
      return await dial('ws')
    }
    const pending = dial(name)
    try {
      const wire = await withTimeout(pending, TRANSPORT_DIAL_TIMEOUT_MS[name], `transport=${name} dial`)
      console.info(`[sense] transport=${name} live`)
      return wire
    } catch (err) {
      // A late-resolving dial must not leak an orphaned live wire.
      void pending.then((w) => w.close(), () => undefined)
      if (opts.transport !== 'auto') throw err
      console.info(`[sense] transport=${name} failed (${errMessage(err)}) — falling back`)
    }
  }
  throw new TransportDialError('no usable transport (auto order exhausted)')
}
