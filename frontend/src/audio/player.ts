/**
 * The one global audio player (single HTMLAudioElement). Every screen plays through this so only one thing
 * sounds at a time and the mini player / Space key always know what's playing.
 *
 *   player.playPreview(track)              20 s clip; prioritises + waits for the preview if it isn't ready
 *   player.play(id, url, { track, startAt }) any audio URL from the API (SetTrack.audio_url, preview audio_url)
 *   player.toggle(id?) / stop() / pause() / resume() / seek(s) / seekFraction(f) / setVolume(v)
 *   usePlayer((s) => s.currentId === id && s.status === "playing")   reactive state
 */
import { create } from "zustand";
import { api, resolveMediaUrl } from "@/api/client";
import type { PreviewStatus, Track } from "@/api/types";
import { useApp } from "@/store/app";
import { toast } from "@/store/toasts";

export type PlayerStatus = "idle" | "loading" | "playing" | "paused" | "error";
export type PlayerSource = "preview" | "file";

export interface PlayerState {
  currentId: string | null;
  url: string | null;
  source: PlayerSource | null;
  track: Track | null;
  status: PlayerStatus;
  position: number; // seconds
  duration: number; // seconds (0 until known)
  volume: number; // 0-1
  error: string | null;
}

const VOLUME_KEY = "seedbox.volume";

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
  source: null,
  track: null,
  status: "idle",
  position: 0,
  duration: 0,
  volume: loadVolume(),
  error: null,
}));

let audio: HTMLAudioElement | null = null;
let raf = 0;
let token = 0; // invalidates async work from a previous play() call
let pendingPreview: { id: string; track: Track | null } | null = null;

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
  audio.addEventListener("loadedmetadata", () => usePlayer.setState({ duration: finite(audio?.duration) }));
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

export interface PlayOptions {
  track?: Track | null;
  source?: PlayerSource;
  startAt?: number; // seconds
}

async function play(id: string, url: string, opts: PlayOptions = {}): Promise<void> {
  const my = ++token;
  pendingPreview = null;
  const a = el();
  usePlayer.setState({
    currentId: id,
    url,
    source: opts.source ?? (url.includes("/previews/") ? "preview" : "file"),
    track: opts.track ?? findTrack(id),
    status: "loading",
    position: opts.startAt ?? 0,
    duration: 0,
    error: null,
  });
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
  pendingPreview = null;
  if (audio) {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }
  stopTicker();
  usePlayer.setState({ currentId: null, url: null, source: null, status: "idle", position: 0, duration: 0, error: null });
}

function previewTrack(t: Track | string): { id: string; track: Track | null } {
  return typeof t === "string" ? { id: t, track: findTrack(t) } : { id: t.id, track: t };
}

/** Play the 20 s preview clip for a track; if it isn't ready yet, prioritise it and start as soon as it is. */
async function playPreview(t: Track | string): Promise<void> {
  const { id, track } = previewTrack(t);
  const p = useApp.getState().previews[id];
  if (p?.state === "ready" && p.audio_url) return play(id, p.audio_url, { track, source: "preview" });
  if (p?.state === "no_match" || (p?.state === "error" && pendingPreview?.id === id)) {
    toast.warning(p.error ?? "No preview available", { key: "player", title: track?.display_name });
    return;
  }
  const my = ++token;
  if (audio) audio.pause();
  pendingPreview = { id, track };
  usePlayer.setState({ currentId: id, url: null, source: "preview", track, status: "loading", position: 0, duration: 0, error: null });
  try {
    const status = await api.prioritizePreview(id, { quiet: true });
    if (my !== token) return;
    useApp.getState().upsertPreview(status);
    onPreview(status);
  } catch (e) {
    if (my !== token) return;
    pendingPreview = null;
    usePlayer.setState({ status: "error", error: (e as Error).message });
    toast.error(`Preview failed: ${(e as Error).message}`, { key: "player" });
  }
}

function onPreview(p: PreviewStatus | undefined): void {
  if (!pendingPreview || !p || p.track_id !== pendingPreview.id) return;
  if (p.state === "ready" && p.audio_url) {
    const { track } = pendingPreview;
    void play(p.track_id, p.audio_url, { track, source: "preview" });
  } else if (p.state === "no_match" || p.state === "error") {
    const name = pendingPreview.track?.display_name;
    pendingPreview = null;
    usePlayer.setState({ status: "error", error: p.error ?? "No preview" });
    toast.warning(p.error ?? "No preview available", { key: "player", title: name });
  }
}

// start pending previews when their SSE `preview` event says ready
useApp.subscribe((s, prev) => {
  if (pendingPreview && s.previews !== prev.previews) onPreview(s.previews[pendingPreview.id]);
});

/** Best-effort lookup of a Track by id from loaded state (for the mini player's label). */
function findTrack(id: string): Track | null {
  const sel = useApp.getState().selection;
  if (sel?.track.id === id) return sel.track;
  const cur = usePlayer.getState();
  if (cur.track?.id === id) return cur.track;
  return null;
}

function isCurrent(id: string): boolean {
  const s = usePlayer.getState();
  return s.currentId === id && (s.status === "playing" || s.status === "loading");
}

export const player = {
  play,
  playPreview,
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
  /** Play/stop a preview (row play buttons): stops if it's the current one, else plays it. */
  togglePreview(t: Track | string): void {
    const { id } = previewTrack(t);
    if (isCurrent(id)) stop();
    else void playPreview(t);
  },
  /** Play/stop a file (set builder rows). */
  toggleFile(id: string, url: string, opts?: PlayOptions): void {
    if (isCurrent(id)) stop();
    else void play(id, url, { source: "file", ...opts });
  },
  seek(seconds: number): void {
    if (!audio) return;
    audio.currentTime = Math.max(0, seconds);
    usePlayer.setState({ position: audio.currentTime });
  },
  seekFraction(f: number): void {
    const d = usePlayer.getState().duration;
    if (d) player.seek(f * d);
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
  /** Space-bar behaviour: play/stop the current selection (falls back to stopping whatever plays). */
  toggleSelection(): void {
    const sel = useApp.getState().selection;
    const s = usePlayer.getState();
    if (!sel) {
      if (s.status === "playing" || s.status === "loading") stop();
      else if (s.status === "paused") player.resume();
      return;
    }
    if (sel.kind === "preview") player.togglePreview(sel.track);
    else player.toggleFile(sel.track.id, sel.url, { track: sel.track });
  },
};

/** True while `id` is the current track and playing or loading. */
export function useIsPlaying(id: string | null | undefined): boolean {
  return usePlayer((s) => !!id && s.currentId === id && (s.status === "playing" || s.status === "loading"));
}
