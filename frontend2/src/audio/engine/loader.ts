/**
 * Streaming: which chunks the plan needs next, and keeping them in the audio thread within the memory budget. A chunk
 * is CHUNK_S seconds of one source. Decks at their worst are 2 x 4 stems, 57 MB of decoded audio per 20 s:
 * HORIZON_S ahead of the playhead fits the budget wherever the chunk boundaries fall, with room for a loop's source.
 *
 * Nothing here looks at the wire: `fetchChunk` brings a chunk's samples, `post` hands them to the audio thread.
 */
import type { Deck } from "../../api/mixplan.generated";
import { Clock } from "./clock";
import { SR } from "./constants";
import { deckTime } from "./position";

export const MAX_DECODED_BYTES = 200 * 1024 * 1024;
export const HORIZON_S = 25;
const STEP_S = 0.25;
const MARGIN_BEATS = 2;
const RETRY_MS = 2000;
const CONCURRENT = 4;

export interface ChunkId {
  key: string;
  index: number;
}

export interface Needed extends ChunkId {
  /** Seconds until it plays. */
  at: number;
}

export interface PlannedDeck {
  plan: Deck;
  keys: string[];
  frames: number;
  chunkFrames: number;
}

/** Every chunk the decks play over the next `horizon` seconds from `t`, soonest first; a loop region wraps. */
export function neededChunks(
  decks: PlannedDeck[],
  clock: Clock,
  t: number,
  end: number,
  loop: { from: number; to: number } | null = null,
  horizon = HORIZON_S,
): Needed[] {
  const seen = new Set<string>();
  const out: Needed[] = [];
  let at = t;
  for (let ahead = 0; ahead <= horizon; ahead += STEP_S, at += STEP_S) {
    if (loop && at >= loop.to) at = loop.from + ((at - loop.from) % (loop.to - loop.from));
    if (at > end) break;
    const beat = clock.beat(at);
    for (const d of decks) {
      // the graph also reads a little past both ends of the play range (its fades, and the tail its filters ring into)
      if (beat < d.plan.play_from_beat - MARGIN_BEATS || beat > d.plan.play_to_beat + MARGIN_BEATS) continue;
      const pos = deckTime(d.plan, beat) * SR;
      if (pos < 0 || pos >= d.frames) continue;
      const index = Math.floor(pos / d.chunkFrames);
      for (const key of d.keys) {
        const id = `${key}#${index}`;
        if (!seen.has(id)) {
          seen.add(id);
          out.push({ key, index, at: ahead });
        }
      }
    }
  }
  return out;
}

export interface LoaderIO {
  /** A chunk's samples; rejects with `ChunkGone` when the source has no such audio any more. */
  fetchChunk: (key: string, index: number) => Promise<{ left: Float32Array; right: Float32Array }>;
  post: (key: string, index: number, left: Float32Array, right: Float32Array) => void;
  drop: (key: string, index: number) => void;
  /** What a chunk of this source takes decoded. */
  chunkBytes: (key: string) => number;
  /** A stem's audio went away (its cache was evicted): the engine falls back to the full track. */
  onGone?: (key: string) => void;
}

/** A source can't serve this chunk any more, and asking again won't help. */
export class ChunkGone extends Error {}

interface Held {
  bytes: number;
  used: number; // when it was last needed: the oldest goes first
}

/** Fetches what's needed and evicts what isn't, never holding (or decoding) more than `maxBytes`. */
export class ChunkLoader {
  private readonly held = new Map<string, Held>();
  private readonly inFlight = new Map<string, number>();
  private readonly failedUntil = new Map<string, number>();
  private readonly waiters: { ids: Set<string>; resolve: () => void }[] = [];
  private tick = 0;
  private needed: Needed[] = [];
  /** Bytes held and being decoded. */
  usedBytes = 0;
  /** The most it has ever been: the test of the budget. */
  peakBytes = 0;

  constructor(
    private readonly io: LoaderIO,
    private readonly maxBytes = MAX_DECODED_BYTES,
    private readonly now: () => number = () => performance.now(),
  ) {}

  /** What the plan wants now. */
  update(needed: Needed[]): void {
    this.needed = needed;
    this.tick++;
    for (const n of needed) {
      const h = this.held.get(`${n.key}#${n.index}`);
      if (h) h.used = this.tick;
    }
    this.pump();
  }

  /** Resolves when these chunks are loaded or have failed for now. */
  whenLoaded(chunks: ChunkId[]): Promise<void> {
    const ids = new Set(chunks.map((c) => `${c.key}#${c.index}`));
    for (const id of ids) if (this.held.has(id)) ids.delete(id);
    if (!ids.size) return Promise.resolve();
    return new Promise((resolve) => {
      this.waiters.push({ ids, resolve });
      this.settle();
    });
  }

  /** Forget a source (a deck got another track): its chunks are dropped from the audio thread. */
  forget(key: string): void {
    for (const [id, h] of this.held) {
      if (id.startsWith(`${key}#`)) {
        this.held.delete(id);
        this.usedBytes -= h.bytes;
        this.io.drop(key, Number(id.slice(key.length + 1)));
      }
    }
  }

  private settle(): void {
    for (let i = this.waiters.length - 1; i >= 0; i--) {
      const w = this.waiters[i];
      for (const id of w.ids) {
        if (this.held.has(id) || this.failedUntil.has(id)) w.ids.delete(id);
      }
      if (!w.ids.size) {
        this.waiters.splice(i, 1);
        w.resolve();
      }
    }
  }

  /** Room for `bytes`, by evicting what the plan doesn't want now, oldest first. */
  private makeRoom(bytes: number): boolean {
    if (this.usedBytes + bytes <= this.maxBytes) return true;
    const wanted = new Set(this.needed.map((n) => `${n.key}#${n.index}`));
    const victims = [...this.held.entries()].filter(([id]) => !wanted.has(id)).sort((a, b) => a[1].used - b[1].used);
    for (const [id, h] of victims) {
      const cut = id.lastIndexOf("#");
      this.held.delete(id);
      this.usedBytes -= h.bytes;
      this.io.drop(id.slice(0, cut), Number(id.slice(cut + 1)));
      if (this.usedBytes + bytes <= this.maxBytes) return true;
    }
    return false;
  }

  private pump(): void {
    const time = this.now();
    for (const n of this.needed) {
      if (this.inFlight.size >= CONCURRENT) return;
      const id = `${n.key}#${n.index}`;
      if (this.held.has(id) || this.inFlight.has(id)) continue;
      if ((this.failedUntil.get(id) ?? 0) > time) continue;
      this.failedUntil.delete(id);
      // twice over while it's on its way: the browser's own AudioBuffer exists next to the copy that goes to the worklet
      const bytes = this.io.chunkBytes(n.key) * 2;
      if (!this.makeRoom(bytes)) return; // soonest first: what's after it can wait until something plays out
      this.usedBytes += bytes;
      this.peakBytes = Math.max(this.peakBytes, this.usedBytes);
      this.inFlight.set(id, bytes);
      void this.load(n.key, n.index, id, bytes);
    }
  }

  private async load(key: string, index: number, id: string, reserved: number): Promise<void> {
    try {
      const { left, right } = await this.io.fetchChunk(key, index);
      this.inFlight.delete(id);
      const bytes = left.byteLength + right.byteLength;
      this.usedBytes += bytes - reserved; // a file's last chunk is shorter than the reservation
      this.held.set(id, { bytes, used: this.tick });
      this.io.post(key, index, left, right);
    } catch (e) {
      this.inFlight.delete(id);
      this.usedBytes -= reserved;
      this.failedUntil.set(id, this.now() + RETRY_MS);
      if (e instanceof ChunkGone) this.io.onGone?.(key);
    }
    this.settle();
    this.pump();
  }
}
