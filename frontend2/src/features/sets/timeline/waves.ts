/** The overview waveforms of a set's tracks, kept outside React: one arriving asks for a repaint, not a render. */
import { apiUrl } from "@/api/client";
import type { WaveData } from "@/features/waveform/prepare";
import { loadWave } from "@/features/waveform/waveData";

const CONCURRENT = 4;
const RETRY_MS = 1500;
const MAX_TRIES = 80; // two minutes: the first request starts the waveform job, when a track has none yet

export class SetWaves {
  private readonly got = new Map<string, WaveData>();
  private readonly queue: string[] = [];
  private readonly wanted = new Set<string>();
  private readonly timers = new Set<ReturnType<typeof setTimeout>>();
  private running = 0;
  private closed = false;

  constructor(
    private readonly onArrive: () => void,
    private readonly load: (url: string) => Promise<WaveData | null> = loadWave,
  ) {}

  get(hash: string): WaveData | null {
    return this.got.get(hash) ?? null;
  }

  /** Starts on whichever of these aren't here or on their way. */
  want(hashes: Iterable<string>): void {
    for (const h of hashes) {
      if (this.wanted.has(h)) continue;
      this.wanted.add(h);
      this.queue.push(h);
    }
    this.pump();
  }

  private pump(): void {
    while (!this.closed && this.running < CONCURRENT && this.queue.length) this.attempt(this.queue.shift()!, 0);
  }

  private attempt(hash: string, tries: number): void {
    this.running++;
    this.load(apiUrl(`/api/tracks/${hash}/waveform`)).then(
      (wave) => {
        this.running--;
        if (this.closed) return;
        if (wave) {
          this.got.set(hash, wave);
          this.onArrive();
        }
        this.pump();
      },
      () => {
        this.running--;
        if (this.closed) return;
        if (tries + 1 < MAX_TRIES) {
          const timer = setTimeout(() => {
            this.timers.delete(timer);
            if (!this.closed) this.attempt(hash, tries + 1);
          }, RETRY_MS);
          this.timers.add(timer);
        }
        this.pump();
      },
    );
  }

  close(): void {
    this.closed = true;
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
  }
}
