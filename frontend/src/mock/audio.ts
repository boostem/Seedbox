/**
 * Mock audio: synthesises a WAV (kick on the beat, hat off-beat, bass on the key's root) at the track's BPM so
 * the player, seeking and progress all work without a backend. Silent-ish volume; cached as blob URLs.
 */
import type { Track } from "@/api/types";
import { POOL } from "./data";
import { fileStem } from "./engine";
import { mockRenders } from "./render";

const urls = new Map<string, string>();
const PCS: Record<string, number> = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };

function rootHz(key: string): number {
  const m = /^([A-G])([♯#♭b]?)/.exec(key);
  let pc = m ? PCS[m[1]] : 9;
  if (m?.[2] === "♯" || m?.[2] === "#") pc++;
  if (m?.[2] === "♭" || m?.[2] === "b") pc--;
  return 55 * 2 ** ((((pc - 9) % 12) + 12) % 12 / 12);
}

function trackFor(path: string): { track: Track | undefined; seconds: number; rate: number } {
  const render = /\/api\/renders\/([^/.]+)\.m4a/.exec(path);
  if (render) {
    // mock transition render: a click track at the master tempo for the render's length
    const info = mockRenders.get(render[1]);
    const pseudo = info ? ({ bpm: info.bpm, key: "A minor" } as Track) : undefined;
    return { track: pseudo, seconds: info?.duration ?? 60, rate: 8000 };
  }
  const prev = /\/api\/previews\/([^/]+)\/audio/.exec(path);
  if (prev) return { track: POOL.get(decodeURIComponent(prev[1])), seconds: 20, rate: 16000 };
  const file = /\/api\/files\/[^/]+\/(.+)$/.exec(path);
  const name = file ? decodeURIComponent(file[1]).replace(/\.mp3$/, "") : "";
  const track = [...POOL.values()].find((t) => fileStem(t) === name);
  return { track, seconds: track?.duration_s ?? 180, rate: 8000 };
}

function synth(track: Track | undefined, seconds: number, rate: number): Blob {
  const bpm = track?.bpm && track.bpm > 60 ? track.bpm : 124;
  const n = Math.floor(seconds * rate);
  const beat = 60 / bpm;
  const f0 = rootHz(track?.key ?? "A minor");
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const w = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF");
  v.setUint32(4, 36 + n * 2, true);
  w(8, "WAVEfmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  w(36, "data");
  v.setUint32(40, n * 2, true);
  let seed = 1;
  for (let i = 0; i < n; i++) {
    const t = i / rate;
    const ph = (t % beat) / beat;
    const tb = ph * beat;
    const kick = Math.sin(2 * Math.PI * (45 * tb + 60 * (1 - Math.exp(-tb * 30)) / 30)) * Math.exp(-tb * 9);
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    const noise = seed / 0x7fffffff - 0.5;
    const hat = Math.abs(ph - 0.5) < 0.04 ? noise * 0.5 : 0;
    const bass = Math.sin(2 * Math.PI * f0 * t) * (ph > 0.5 ? 0.35 : 0.1);
    const s = (kick * 0.55 + hat * 0.25 + bass * 0.35) * 0.5;
    v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 32767, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

export function mockAudioUrl(path: string): string {
  const hit = urls.get(path);
  if (hit) return hit;
  const { track, seconds, rate } = trackFor(path);
  const url = URL.createObjectURL(synth(track, seconds, rate));
  urls.set(path, url);
  if (urls.size > 8) {
    const [k, old] = urls.entries().next().value as [string, string];
    URL.revokeObjectURL(old);
    urls.delete(k);
  }
  return url;
}
