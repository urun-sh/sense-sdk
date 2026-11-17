/** `session.attach(stream)` — one call to stream a live mic (and camera)
 * into a session. Ported from the console's realtime playground. */
import { JPEG_QUALITIES, PCM_WORKLET_SOURCE, VIDEO_MAX_BYTES, VIDEO_TARGET_WIDTH } from "./media.ts";

/** Minimal structural view of a browser MediaStream (keeps DOM lib types out
 * of the published declarations; a real MediaStream satisfies it). */
export interface AttachStream {
  getAudioTracks(): unknown[];
  getVideoTracks(): unknown[];
}

/** What attach needs from a session: the two input buses and the frame
 * cadence. `Session` implements this; plain objects do for tests. */
export interface AttachTarget {
  readonly audio: { push(pcm: Uint8Array | ArrayBuffer, source?: string): void };
  readonly video: { push(jpeg: Uint8Array | ArrayBuffer): void };
  readonly hz: number;
}

export interface AttachHandle {
  /** Stop sampling and pushing. Does NOT stop the stream's tracks — the
   * caller owns them (stop them yourself to turn the mic light off). */
  stop(): void;
}

// --- structural browser-API views (DOM lib types are not a dependency) -----

export interface AudioWorkletPortLike {
  onmessage: ((ev: { data: unknown }) => void) | null;
}

export interface AudioNodeLike {
  connect(dest: unknown): AudioNodeLike;
}

export interface AudioContextLike {
  readonly sampleRate: number;
  readonly destination: unknown;
  readonly audioWorklet: { addModule(url: string): Promise<void> };
  createMediaStreamSource(stream: AttachStream): AudioNodeLike;
  createGain(): { gain: { value: number } } & AudioNodeLike;
  close(): Promise<void>;
}

export interface VideoElementLike {
  srcObject: AttachStream | null;
  muted: boolean;
  playsInline: boolean;
  play(): Promise<void>;
  videoWidth: number;
  videoHeight: number;
  readyState: number;
}

export interface CanvasLike {
  width: number;
  height: number;
  getContext(kind: string): { drawImage(img: VideoElementLike, x: number, y: number, w: number, h: number): void } | null;
  toBlob(cb: (blob: { size: number; arrayBuffer(): Promise<ArrayBuffer> } | null) => void, type: string, quality?: number): void;
}

export interface BlobLike {
  readonly size: number;
  arrayBuffer(): Promise<ArrayBuffer>;
}

interface AudioWorkletNodeCtor {
  new (ctx: unknown, name: string, opts?: { numberOfOutputs?: number }): {
    port: AudioWorkletPortLike;
    connect(dest: unknown): AudioNodeLike;
  };
}

/** @internal test seams — production code never passes these. */
export interface AttachOptions {
  audioContext?: (sampleRate: number) => AudioContextLike;
  canvas?: () => CanvasLike;
}


export async function attachStream(
  target: AttachTarget,
  stream: AttachStream,
  opts: AttachOptions = {},
): Promise<AttachHandle> {
  const g = globalThis as Record<string, unknown>;
  const stops: (() => void)[] = [];
  let stopped = false;
  const handle: AttachHandle = {
    stop() {
      if (stopped) return;
      stopped = true;
      for (let i = stops.length - 1; i >= 0; i--) stops[i]!();
    },
  };

  // --- audio: AudioContext resamples the mic to 24 kHz; the worklet frames
  // 40 ms PCM16 chunks and each one is pushed as it lands.
  if (stream.getAudioTracks().length > 0) {
    const Ctor = g.AudioContext as (new (opts: { sampleRate: number }) => AudioContextLike) | undefined;
    if (!opts.audioContext && !Ctor) {
      throw new Error(
        "session.attach() is browser-only (AudioContext not available). " +
          "In Node, push PCM16 24 kHz mono chunks yourself: session.audio.push(pcm).",
      );
    }
    const ctx = opts.audioContext ? opts.audioContext(24_000) : new Ctor!({ sampleRate: 24_000 });
    stops.push(() => void ctx.close().catch(() => {}));
    const url = moduleUrl(PCM_WORKLET_SOURCE);
    stops.push(() => {
      const revoke = (g.URL as { revokeObjectURL?(u: string): void } | undefined)?.revokeObjectURL;
      if (revoke) revoke(url);
    });
    await ctx.audioWorklet.addModule(url);
    const NodeCtor = g.AudioWorkletNode as AudioWorkletNodeCtor | undefined;
    if (!NodeCtor) throw new Error("session.attach(): AudioWorkletNode not available in this browser.");
    const node = new NodeCtor(ctx, "sense-pcm", { numberOfOutputs: 1 });
    node.port.onmessage = (ev) => {
      if (stopped) return;
      const chunk = ev.data as { buffer: ArrayBuffer; byteOffset: number; byteLength: number }; // our own injected worklet's Int16Array message
      target.audio.push(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength));
    };
    ctx.createMediaStreamSource(stream).connect(node);
    // Pulled only if the graph reaches the destination — through zero gain,
    // so the mic never feeds back to the speakers.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);
  }

  // --- video: sample frames to JPEG at the session cadence with drop-older
  // backpressure (never queue frames).
  if (stream.getVideoTracks().length > 0) {
    const doc = g.document as { createElement(tag: string): unknown } | undefined; // browser DOM only — unknown until here
    if (!doc) {
      console.warn("session.attach(): no DOM — video frames skipped.");
    } else {
      const video = doc.createElement("video") as VideoElementLike; // our own element, well-known shape
      video.srcObject = stream;
      video.muted = true;
      video.playsInline = true;
      try {
        await video.play();
      } catch {
        // Autoplay policy — frames flow once the element is visible/played.
      }
      let busy = false;
      const timer = setInterval(() => {
        if (stopped || busy || video.readyState < 2) return;
        busy = true;
        void captureJpeg(video, opts)
          .then(async (blob) => {
            if (!blob) return;
            const buf = await blob.arrayBuffer();
            busy = false;
            if (!stopped) target.video.push(buf);
          })
          .catch(() => {
            busy = false;
          });
      }, 1000 / target.hz);
      stops.push(() => clearInterval(timer));
    }
  }

  return handle;
}

/** Blob URL when the platform has it (browsers); data URL otherwise (tests). */
function moduleUrl(source: string): string {
  const g = globalThis as { Blob?: typeof Blob; URL?: typeof URL };
  if (g.Blob && g.URL && typeof g.URL.createObjectURL === "function") {
    return g.URL.createObjectURL(new g.Blob([source], { type: "application/javascript" }));
  }
  return `data:text/javascript;base64,${btoa(source)}`;
}

/** One JPEG frame ≤ VIDEO_MAX_BYTES: draw at VIDEO_TARGET_WIDTH wide, step
 * quality down until it fits. None fit → null (skip this frame). */
async function captureJpeg(video: VideoElementLike, opts: AttachOptions): Promise<BlobLike | null> {
  const doc = (globalThis as { document?: { createElement(tag: string): unknown } }).document; // browser DOM only — unknown until here
  const canvas = opts.canvas ? opts.canvas() : (doc!.createElement("canvas") as CanvasLike); // our own element, well-known shape
  const scale = VIDEO_TARGET_WIDTH / Math.max(video.videoWidth || VIDEO_TARGET_WIDTH, 1);
  canvas.width = Math.round((video.videoWidth || VIDEO_TARGET_WIDTH) * scale);
  canvas.height = Math.round((video.videoHeight || (VIDEO_TARGET_WIDTH * 9) / 16) * scale);
  canvas.getContext("2d")?.drawImage(video, 0, 0, canvas.width, canvas.height);
  for (const quality of JPEG_QUALITIES) {
    const { promise, resolve } = Promise.withResolvers<BlobLike | null>();
    canvas.toBlob(resolve, "image/jpeg", quality);
    const blob = await promise;
    if (blob && blob.size <= VIDEO_MAX_BYTES) return blob;
  }
  return null;
}

