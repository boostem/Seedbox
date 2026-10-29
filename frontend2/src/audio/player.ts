/**
 * The one global audio player (single HTMLAudioElement), so only one thing sounds at a time. The waveforms draw
 * their playhead from `usePlayer`. Seedbox 2's AudioWorklet engine (#65) replaces it.
 *
 *   player.play(id, url, { startAt })   any audio URL from the API
 *   player.toggle(id?) / stop() / pause() / resume() / seek(s) / setVolume(v)
 *   usePlayer((s) => s.currentId === id && s.status === "playing")   reactive state
 */
import { create } from "zustand";
import { resolveMediaUrl } from "@/api/client";
import { toast } from "@/store/toasts";

export type PlayerStatus = "idle" | "loading" | "playing" | "paused" | "error";

export interface PlayerState {
  currentId: string | null;
  url: string | null;
  status: PlayerStatus;
  position: number; // seconds
  duration: number; // seconds (0 until known)
  volume: number; // 0-1
  error: string | null;
}

const VOLUME_KEY = "seedbox2.volume";

function loadVolume(): number {
  try {
    const v = Number(localStorage.getItem(VOLUME_KEY));
    return Number.isFinite(v) && v > 0 && v <= 1 ? v : 0.8;
  } catch {
    return 0.8;
  }
}

export const usePlayer = create<PlayerState>()(() => ({
  currentId: null,
  url: null,
  status: "idle",
  position: 0,
  duration: 0,
  volume: loadVolume(),
  error: null,
}));

let audio: HTMLAudioElement | null = null;
let raf = 0;
let token = 0; // invalidates async work from a previous play() call

function el(): HTMLAudioElement {
  if (audio) return audio;
  audio = new Audio();
  audio.preload = "auto";
  audio.volume = usePlayer.getState().volume;
  audio.addEventListener("playing", () => {
    usePlayer.setState({ status: "playing", error: null });
    startTicker();
  });
  audio.addEventListener("pause", () => {
    if (usePlayer.getState().status === "playing") usePlayer.setState({ status: "paused" });
    stopTicker();
  });
  audio.addEventListener("waiting", () => usePlayer.setState({ status: "loading" }));
  audio.addEventListener("durationchange", () => usePlayer.setState({ duration: finite(audio?.duration) }));
  audio.addEventListener("ended", () => {
    stopTicker();
    usePlayer.setState({ status: "idle", position: 0 });
  });
  audio.addEventListener("error", () => {
    if (!usePlayer.getState().currentId) return;
    stopTicker();
    const msg = "Couldn't play this audio";
    usePlayer.setState({ status: "error", error: msg });
    toast.error(msg, { key: "player" });
  });
  return audio;
}

function finite(n: number | undefined): number {
  return n && Number.isFinite(n) ? n : 0;
}

let lastTick = 0;
function startTicker() {
  stopTicker();
  const tick = (t: number) => {
    if (audio && t - lastTick > 40) {
      lastTick = t;
      usePlayer.setState({ position: audio.currentTime });
    }
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
}
function stopTicker() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  if (audio) usePlayer.setState({ position: audio.currentTime });
}

async function play(id: string, url: string, opts: { startAt?: number } = {}): Promise<void> {
  const my = ++token;
  const a = el();
  usePlayer.setState({ currentId: id, url, status: "loading", position: opts.startAt ?? 0, duration: 0, error: null });
  try {
    const src = await resolveMediaUrl(url);
    if (my !== token) return;
    a.src = src;
    if (opts.startAt) a.currentTime = opts.startAt;
    await a.play();
  } catch (e) {
    if (my !== token || (e as Error).name === "AbortError") return;
    usePlayer.setState({ status: "error", error: (e as Error).message });
  }
}

function stop(): void {
  token++;
  if (audio) {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }
  stopTicker();
  usePlayer.setState({ currentId: null, url: null, status: "idle", position: 0, duration: 0, error: null });
}

function isCurrent(id: string): boolean {
  const s = usePlayer.getState();
  return s.currentId === id && (s.status === "playing" || s.status === "loading");
}

export const player = {
  play,
  stop,
  pause: () => audio?.pause(),
  resume: () => {
    if (audio?.src) void audio.play();
  },
  /** Stop if `id` (or anything, when omitted) is playing/loading; otherwise resume a paused clip. */
  toggle(id?: string): void {
    const s = usePlayer.getState();
    if (s.status === "playing" || s.status === "loading") {
      if (!id || s.currentId === id) return stop();
    } else if (s.status === "paused" && (!id || s.currentId === id)) {
      return player.resume();
    }
  },
  seek(seconds: number): void {
    if (!audio) return;
    audio.currentTime = Math.max(0, seconds);
    usePlayer.setState({ position: audio.currentTime });
  },
  setVolume(v: number): void {
    const vol = Math.min(1, Math.max(0, v));
    if (audio) audio.volume = vol;
    usePlayer.setState({ volume: vol });
    try {
      localStorage.setItem(VOLUME_KEY, String(vol));
    } catch {
      /* ignore */
    }
  },
  isCurrent,
};

/** True while `id` is the current track and playing or loading. */
export function useIsPlaying(id: string | null | undefined): boolean {
  return usePlayer((s) => !!id && s.currentId === id && (s.status === "playing" || s.status === "loading"));
}
