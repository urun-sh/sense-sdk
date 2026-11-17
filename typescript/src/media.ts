/** Shared media-capture constants and helpers for `session.attach()`.

 * The AudioWorklet (`PCM_WORKLET_SOURCE`) is injected into the page as a
 * self-contained blob and cannot import this module, so its conversion and
 * framing math is mirrored here in `floatToPcm16`/`PcmFramer`;
 * `test/attach.test.ts` pins the two together so they cannot drift. */

/** Samples per 40 ms PCM16 chunk at 24 kHz — the SDK's audio framing. */
export const PCM_CHUNK_SAMPLES = 960;

/** One JPEG frame must fit this budget or it is dropped (§ wire frame cap). */
export const VIDEO_MAX_BYTES = 250_000;
/** Video frames are drawn scaled to this width before encoding. */
export const VIDEO_TARGET_WIDTH = 512;
/** Quality ladder: first rung that fits wins; none fit → frame skipped. */
export const JPEG_QUALITIES = [0.72, 0.5, 0.35] as const;

/** Float32 → PCM16 with the same clamp/scale the worklet applies. */
export function floatToPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]!));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

/** Accumulate arbitrary-sized float32 chunks; emit fixed 960-sample PCM16
 * frames (the SDK's 40 ms audio framing). The sub-40 ms tail is held. */
export class PcmFramer {
  private carry = new Float32Array(0);

  push(chunk: Float32Array): Int16Array[] {
    const data = this.carry.length ? this.concat(this.carry, chunk) : chunk;
    const frames: Int16Array[] = [];
    let off = 0;
    while (off + PCM_CHUNK_SAMPLES <= data.length) {
      frames.push(floatToPcm16(data.subarray(off, off + PCM_CHUNK_SAMPLES)));
      off += PCM_CHUNK_SAMPLES;
    }
    this.carry = data.slice(off);
    return frames;
  }

  private concat(a: Float32Array, b: Float32Array): Float32Array {
    const out = new Float32Array(a.length + b.length);
    out.set(a);
    out.set(b, a.length);
    return out;
  }
}

/** The injected worklet: the AudioContext is created at 24 kHz so the browser
 * resamples the mic stream itself; this processor converts to Int16 and emits
 * 40 ms (960-sample) chunks. Mirror of `floatToPcm16` + `PcmFramer` above. */
export const PCM_WORKLET_SOURCE = `
class SensePcm extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(960); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === 960) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("sense-pcm", SensePcm);
`;
