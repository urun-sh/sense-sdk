import { describe, expect, test, vi } from "bun:test";
import { PCM_CHUNK_SAMPLES, PCM_WORKLET_SOURCE, PcmFramer, floatToPcm16 } from "../src/media.ts";
import {
  attachStream,
  type AudioContextLike,
  type AttachStream,
  type AttachTarget,
  type CanvasLike,
} from "../src/attach.ts";

// --- fakes -----------------------------------------------------------------

class FakePort {
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  postMessage(data: unknown): void {
    if (this.onmessage) this.onmessage({ data });
  }
}

type WorkletProc = { process(inputs: Float32Array[][]): boolean };
type WorkletCtor = new () => { port: unknown; process: WorkletProc["process"] };

/** Run the REAL worklet source (the string attach injects as a blob) and
 * hand back the registered processor class — no AudioWorklet needed. */
/** Minimal AudioWorkletProcessor base for evaluating the real worklet source. */
class FakeProcessor {
  constructor(_opts?: unknown) {}
}

function evalWorklet(): Record<string, WorkletCtor> {
  const registry: Record<string, WorkletCtor> = {};
  const register = (name: string, cls: unknown) => {
    registry[name] = cls as WorkletCtor; // the source is SDK-authored; the cast binds it to the shape attach drives
  };
  new Function("registerProcessor", "AudioWorkletProcessor", PCM_WORKLET_SOURCE)(register, FakeProcessor);
  return registry;
}

function fakeAudioContext() {
  const registry = evalWorklet();
  let closed = false;
  let sourceTarget: { proc: WorkletProc; port: FakePort } | null = null;
  const ctx = {
    sampleRate: 24000,
    audioWorklet: {
      addModule: async (url: string) => {
        const src = Buffer.from(url.replace("data:text/javascript;base64,", ""), "base64").toString();
        const register = (name: string, cls: unknown) => {
          registry[name] = cls as WorkletCtor; // same shape as evalWorklet — the module URL carries the same source
        };
        new Function("registerProcessor", "AudioWorkletProcessor", src)(register, FakeProcessor);
      },
    },
    destination: { kind: "destination" },
    createMediaStreamSource: (_stream: unknown) => ({
      connect(node: unknown) {
        sourceTarget = node as { proc: WorkletProc; port: FakePort }; // the node we install is the FakeNode below
        return node;
      },
    }),
    createGain: () => ({ gain: { value: 0 }, connect(dest: unknown) { return dest; } }),
    close: async () => {
      closed = true;
    },
  };
  return { ctx, registry, wasClosed: () => closed, getSourceTarget: () => sourceTarget };
}

/** Global AudioWorkletNode stub: instantiates the processor the fake ctx
 * registered and wires its port through to attach's onmessage. */
function installFakeNodeCtor(registry: Record<string, unknown>): void {
  class FakeNode {
    port = new FakePort();
    proc: { process: WorkletProc["process"] };
    constructor(_ctx: unknown, name: string) {
      const Cls = registry[name] as WorkletCtor; // registered by evalWorklet/addModule above
      const worklet = new Cls();
      // The worklet class owns its port; re-home it so attach's
      // `node.port.onmessage = …` reaches the processor.
      worklet.port = this.port;
      this.proc = worklet;
    }
    connect(dest: unknown) {
      return dest;
    }
  }
  (globalThis as { AudioWorkletNode?: unknown }).AudioWorkletNode = FakeNode;
}

const audioOnly = { getAudioTracks: () => [{ stop() {} }], getVideoTracks: () => [] } as AttachStream; // plain objects satisfy the structural MediaStream view

/** Drain pending promise chains (bun:test fake timers have no async advance). */
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

/** Bun hands out unreadable blob URLs, so hide Blob while attaching to force
 * attach's data-URL fallback — the only path the fake ctx can read back. */
function hideBlob(): () => void {
  const g = globalThis as { Blob?: unknown };
  const prev = g.Blob;
  g.Blob = undefined;
  return () => {
    g.Blob = prev;
  };
}
const videoOnly = { getAudioTracks: () => [], getVideoTracks: () => [{ stop() {} }] } as AttachStream;

interface RecordingTarget extends AttachTarget {
  pushedAudio: Uint8Array[];
  pushedVideo: (Uint8Array | ArrayBuffer)[];
}

function recordingTarget(): RecordingTarget {
  const pushedAudio: Uint8Array[] = [];
  const pushedVideo: (Uint8Array | ArrayBuffer)[] = [];
  return {
    hz: 12.5,
    audio: {
      push: (pcm) => pushedAudio.push(pcm instanceof Uint8Array ? pcm : new Uint8Array(pcm)),
    },
    video: { push: (jpeg) => pushedVideo.push(jpeg) },
    pushedAudio,
    pushedVideo,
  };
}

/** Attach with the real worklet source flowing through the fake graph. */
async function attachAudio(target: AttachTarget) {
  const { ctx, registry, wasClosed, getSourceTarget } = fakeAudioContext();
  installFakeNodeCtor(registry);
  const restore = hideBlob();
  const handle = await attachStream(target, audioOnly, {
    audioContext: () => ctx as AudioContextLike, // the fake mirrors only the AudioContext surface attach touches
  });
  restore(); // blob only matters while the worklet module URL is built
  const node = getSourceTarget()!;
  expect(node).toBeTruthy();
  return { handle, node, wasClosed };
}

// --- tests -----------------------------------------------------------------

describe("session.attach", () => {
  test("resamples to 40 ms PCM16 chunks: 128-sample render quanta frame at 960", async () => {
    const target = recordingTarget();
    const { handle, node } = await attachAudio(target);
    const samples: number[] = [];
    for (let i = 0; i < 1280; i++) samples.push(Math.sin(i / 5));
    // The real Web Audio render quantum is 128 frames; feed it that way.
    for (let off = 0; off < samples.length; off += 128) {
      const end = Math.min(off + 128, samples.length);
      const chunk = Float32Array.from({ length: 128 }, (_, i) => (off + i < end ? samples[off + i]! : 0));
      node.proc.process([[chunk]]);
    }
    expect(target.pushedAudio.length).toBe(Math.floor(1280 / PCM_CHUNK_SAMPLES)); // 1
    const frame = target.pushedAudio[0]!;
    expect(frame.byteLength).toBe(PCM_CHUNK_SAMPLES * 2);
    const expected = floatToPcm16(Float32Array.from(samples.slice(0, PCM_CHUNK_SAMPLES)));
    expect(Array.from(new Int16Array(frame.buffer, frame.byteOffset, 960))).toEqual(Array.from(expected));

    handle.stop();
  });

  test("worklet output matches PcmFramer on arbitrary chunk sizes", async () => {
    const target = recordingTarget();
    const { handle, node } = await attachAudio(target);
    const framer = new PcmFramer();
    let seed = 42;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648) * 2 - 1;
    let fed = 0;
    let frameIndex = 0;
    for (const size of [1, 100, 128, 512, 333, 128, 5, 2000]) {
      const chunk = Float32Array.from({ length: size }, rnd);
      node.proc.process([[chunk]]);
      for (const f of framer.push(chunk)) {
        const got = target.pushedAudio[frameIndex++]!;
        expect(new Int16Array(f.buffer, f.byteOffset, f.byteLength / 2)).toEqual(
          new Int16Array(got.buffer, got.byteOffset, got.byteLength / 2),
        );
      }
      fed += size;
    }
    expect(target.pushedAudio.length).toBe(Math.floor(fed / PCM_CHUNK_SAMPLES));
    handle.stop();
  });

  test("stop() closes the context and drops later audio", async () => {
    const target = recordingTarget();
    const { handle, node, wasClosed } = await attachAudio(target);
    expect(wasClosed()).toBe(false);
    handle.stop();
    expect(wasClosed()).toBe(true);
    node.proc.process([[Float32Array.from({ length: 960 }, () => 0.5)]]);
    expect(target.pushedAudio.length).toBe(0);
    handle.stop(); // idempotent
  });
});

describe("session.attach video & guards", () => {
  test("video: JPEG frames at the session cadence, quality ladder skips oversized", async () => {
    const target = recordingTarget();
    const qualities: number[] = [];
    let canvasCount = 0;
    const fakeVideo = {
      srcObject: null as unknown,
      muted: false,
      playsInline: false,
      play: async () => {},
      videoWidth: 1280,
      videoHeight: 720,
      readyState: 2,
    };
    const fakeCanvas = (): CanvasLike => ({
      width: 0,
      height: 0,
      getContext: (_kind: string) => ({ drawImage() {} }),
      toBlob: (cb, _type, q) => {
        qualities.push(q ?? -1);
        const big = (q ?? 1) >= 0.5; // first two rungs too large, third fits
        cb({ size: big ? 300_000 : 1_000, arrayBuffer: async () => new ArrayBuffer(big ? 300_000 : 1_000) });
      },
    });
    const g = globalThis as { document?: unknown };
    const prevDoc = g.document;
    g.document = {
      createElement: (tag: string) => {
        if (tag === "video") {
          canvasCount++;
          return fakeVideo;
        }
        return fakeCanvas();
      },
    };
    // The sampling interval is created inside attach, so fake the clock
    // before attaching; the capture path resolves synchronously here, so no
    // wall time is needed.
    vi.useFakeTimers();
    try {
      const restoreBlob = hideBlob();
      try {
        const handle = await attachStream(target, videoOnly, { canvas: fakeCanvas });
        expect(fakeVideo.srcObject).toBe(videoOnly);
        expect(canvasCount).toBe(1);
        await vi.advanceTimersByTime(350);
        await flushMicrotasks(); // drain the toBlob → arrayBuffer → push chain
        expect(target.pushedVideo.length).toBeGreaterThanOrEqual(3);
        expect(target.pushedVideo[0]!.byteLength).toBe(1_000);
        expect(qualities).toContain(0.35); // ladder stepped down to fit
        handle.stop();
        const at = target.pushedVideo.length;
        await vi.advanceTimersByTime(200);
        await flushMicrotasks();
        expect(target.pushedVideo.length).toBe(at);
      } finally {
        restoreBlob();
      }
    } finally {
      vi.useRealTimers();
      g.document = prevDoc;
    }
  });

  test("without a browser, the error points at session.audio.push", async () => {
    const g = globalThis as { AudioContext?: unknown };
    const prev = g.AudioContext;
    delete g.AudioContext;
    try {
      await expect(attachStream(recordingTarget(), audioOnly)).rejects.toThrow(/browser[\s\S]*session\.audio\.push/);
    } finally {
      g.AudioContext = prev;
    }
  });
});
