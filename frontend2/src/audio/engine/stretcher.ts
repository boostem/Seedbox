/**
 * Signalsmith Stretch, the renderer's engine (`seedbox2/mix/stretch.py`), as WebAssembly. An instance holds one stretcher,
 * so a deck that stretches takes one from the pool.
 *
 * Everything here is synchronous and uses nothing an AudioWorkletGlobalScope lacks (it has no `atob`, `crypto` or `fetch`).
 * The random seed is fixed, so that the same plan gives the same samples, in the worklet and out of it.
 */
import { SR } from "./constants";
import { STRETCH_WASM_BASE64 } from "./stretch-wasm.generated";

export interface StretchProfile {
  blockS: number;
  intervalS: number;
}

/** Signalsmith's default preset, as the renderer's `FULL`. */
export const NORMAL_PROFILE: StretchProfile = { blockS: 0.12, intervalS: 0.03 };
/** Low-power mode: Signalsmith's "cheaper" block, at two thirds of the spectra a second the normal profile works out. */
export const LOW_POWER_PROFILE: StretchProfile = { blockS: 0.1, intervalS: 0.045 };

/** Above this a shift doesn't keep partials' harmonic relations (Signalsmith's own default, as in the renderer). */
const TONALITY = 8000 / SR;
/** Samples per channel of the buffers shared with the stretcher: more than a seek's pre-roll or a block at any ratio. */
export const STRETCH_BUFFER = 16384;

/** The build's minified exports, for this version of the package. */
interface Exports {
  e: WebAssembly.Memory;
  f(): void; // constructors
  h(channels: number, length: number): number; // setBuffers
  k(): number; // inputLatency
  l(): number; // outputLatency
  m(): void; // reset
  p(channels: number, block: number, interval: number, split: number): void; // configure
  q(factor: number, tonality: number): void; // setTransposeFactor
  v(samples: number, rate: number): void; // seek
  w(input: number, output: number): void; // process
  y(argc: number, argv: number): number; // main
}

const BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

function decodeBase64(text: string): Uint8Array<ArrayBuffer> {
  const values = new Uint8Array(128);
  for (let i = 0; i < BASE64.length; i++) values[BASE64.charCodeAt(i)] = i;
  const padding = text.endsWith("==") ? 2 : text.endsWith("=") ? 1 : 0;
  const out = new Uint8Array((text.length * 3) / 4 - padding);
  let o = 0;
  for (let i = 0; i < text.length; i += 4) {
    const n =
      (values[text.charCodeAt(i)] << 18) |
      (values[text.charCodeAt(i + 1)] << 12) |
      (values[text.charCodeAt(i + 2)] << 6) |
      values[text.charCodeAt(i + 3)];
    if (o < out.length) out[o++] = (n >> 16) & 255;
    if (o < out.length) out[o++] = (n >> 8) & 255;
    if (o < out.length) out[o++] = n & 255;
  }
  return out;
}

let compiled: WebAssembly.Module | null = null;

/** Compiles the stretcher. On first use otherwise: the worklet calls it at its start, before there's sound to interrupt. */
export function compileStretcher(): WebAssembly.Module {
  return (compiled ??= new WebAssembly.Module(decodeBase64(STRETCH_WASM_BASE64)));
}

export class Stretcher {
  /** Input samples ahead of the place in the source that a spectrum is centred on. */
  inputLatency = 0;
  /** Output samples from a spectrum's being worked out to its centre coming out. */
  outputLatency = 0;
  /** Input samples a seek wants: one block and one interval. */
  seekSamples = 0;
  /** Output samples before what comes out is fully overlapped, after a seek. */
  settleSamples = 0;
  private readonly exports: Exports;
  private buffers = 0;
  private profile: StretchProfile | null = null;
  private views: Float32Array[] = [];
  private transposed = NaN;

  constructor() {
    const memory = (): Uint8Array => new Uint8Array(this.exports.e.buffer);
    let seed = 0x2f6e2b1;
    const imports = {
      a: {
        // random_get: the seed of the stretcher's random engine
        a: (at: number, size: number): number => {
          const heap = memory();
          for (let i = 0; i < size; i++) {
            seed = Math.imul(seed, 1664525) + 1013904223;
            heap[at + i] = seed >>> 24;
          }
          return 0;
        },
        // emscripten_resize_heap
        b: (size: number): number => {
          const m = this.exports.e;
          const pages = Math.ceil((size >>> 0) / 65536) - m.buffer.byteLength / 65536;
          m.grow(Math.max(pages, 0));
          return 1;
        },
        // memcpy
        c: (to: number, from: number, n: number): void => {
          memory().copyWithin(to, from, from + n);
        },
        d: (): void => {
          throw new Error("the stretcher aborted");
        },
      },
    };
    const instance = new WebAssembly.Instance(compileStretcher(), imports);
    this.exports = instance.exports as unknown as Exports;
    this.exports.f();
    this.exports.y(0, 0);
  }

  /** The block and interval, as the renderer's `_Engine` sets them. Spectra are shared out over the interval's blocks. */
  configure(profile: StretchProfile): void {
    if (this.profile !== profile) {
      const { exports: x } = this;
      x.p(2, Math.round(profile.blockS * SR), Math.round(profile.intervalS * SR), 1);
      this.buffers = x.h(2, STRETCH_BUFFER);
      this.inputLatency = x.k();
      this.outputLatency = x.l();
      const block = Math.round(profile.blockS * SR);
      const interval = Math.round(profile.intervalS * SR);
      this.seekSamples = block + interval;
      this.settleSamples = block + interval;
      this.profile = profile;
      this.views = [];
    }
    this.exports.m();
    this.transposed = NaN;
  }

  /** What it's configured for. */
  get profileNow(): StretchProfile {
    return this.profile!;
  }

  /** The memory can grow under a view, and that empties it. */
  private view(i: number): Float32Array {
    let view = this.views[i];
    if (!view || view.length === 0) {
      view = new Float32Array(this.exports.e.buffer, this.buffers + i * STRETCH_BUFFER * 4, STRETCH_BUFFER);
      this.views[i] = view;
    }
    return view;
  }

  /** Where input goes, channel 0 or 1. */
  input(channel: number): Float32Array {
    return this.view(channel);
  }

  /** Where output comes, channel 0 or 1. */
  output(channel: number): Float32Array {
    return this.view(2 + channel);
  }

  /** Pitch: the source's times `factor`. */
  transpose(factor: number): void {
    if (factor === this.transposed) return;
    this.transposed = factor;
    this.exports.q(factor, TONALITY);
  }

  /** Starts from the `samples` in the input buffers: the ones just before where it goes on from. */
  seek(samples: number, rate: number): void {
    this.exports.v(samples, rate);
  }

  /** `inSamples` from the input buffers to `outSamples` in the output buffers: their ratio is the time stretch. */
  process(inSamples: number, outSamples: number): void {
    this.exports.w(inSamples, outSamples);
  }
}

const pool: Stretcher[] = [];

export function takeStretcher(profile: StretchProfile): Stretcher {
  const stretcher = pool.pop() ?? new Stretcher();
  stretcher.configure(profile);
  return stretcher;
}

export function giveBackStretcher(stretcher: Stretcher): void {
  pool.push(stretcher);
}
