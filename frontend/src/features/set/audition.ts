/**
 * "Audition transition" — a ROUGH preview of an A → B mix, local to the set builder.
 *
 * Why not the global player: it owns exactly one <audio>, and a mix needs two decks. So this module keeps its own
 * two HTMLAudioElements (A and B), and stops the global player while it runs (and stops itself as soon as the
 * global player starts again), so only one thing ever sounds.
 *
 * What it does: plays A from ~8 bars before out_start_s; when A reaches out_start_s it starts B at in_start_s
 * (playbackRate = 1 + pitch_pct/100, key lock on), crossfades linearly over the overlap (B up in the first half,
 * A down in the second; a cut just switches), and stops 8 bars after A is out. Start sync is rAF-polled, so expect
 * tens of ms of drift: it tells you whether the parts fit, not whether the beats are locked.
 */
import { create } from "zustand";
import { resolveMediaUrl } from "@/api/client";
import { player, usePlayer } from "@/audio/player";
import { toast } from "@/store/toasts";

export interface AuditionPlan {
  key: string; // "from>to"
  aUrl: string;
  bUrl: string;
  out: number; // A file time where B comes in
  inStart: number; // B file time to start from
  rate: number; // B playback rate
  overlapS: number; // 0 = cut
  preRollS: number;
  tailS: number;
}

export interface AuditionState {
  key: string | null;
  status: "idle" | "loading" | "playing";
  /** Current mix time (= A's file time axis), while playing. */
  mix: number | null;
}

export const useAudition = create<AuditionState>()(() => ({ key: null, status: "idle", mix: null }));

let A: HTMLAudioElement | null = null;
let B: HTMLAudioElement | null = null;
let raf = 0;
let token = 0;

function deck(): [HTMLAudioElement, HTMLAudioElement] {
  if (!A) {
    A = new Audio();
    A.preload = "auto";
  }
  if (!B) {
    B = new Audio();
    B.preload = "auto";
  }
  return [A, B];
}

function once(el: HTMLAudioElement, ev: string, ms = 8000): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      cleanup();
      reject(new Error("audio took too long to load"));
    }, ms);
    const ok = () => {
      cleanup();
      resolve();
    };
    const bad = () => {
      cleanup();
      reject(new Error("couldn't load the audio"));
    };
    const cleanup = () => {
      clearTimeout(t);
      el.removeEventListener(ev, ok);
      el.removeEventListener("error", bad);
    };
    el.addEventListener(ev, ok);
    el.addEventListener("error", bad);
  });
}

async function seekTo(el: HTMLAudioElement, t: number): Promise<void> {
  if (el.readyState < 1) await once(el, "loadedmetadata");
  const target = Math.max(0, Math.min(t, (el.duration || t + 1) - 0.05));
  if (Math.abs(el.currentTime - target) < 0.01) return;
  const done = once(el, "seeked");
  el.currentTime = target;
  await done;
}

function silence() {
  cancelAnimationFrame(raf);
  raf = 0;
  for (const el of [A, B]) {
    if (!el) continue;
    el.pause();
  }
}

function stop(): void {
  token++;
  silence();
  if (useAudition.getState().status !== "idle") useAudition.setState({ key: null, status: "idle", mix: null });
}

const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);

/** Start the audition (optionally from mix time `from`, e.g. a click on the mix view). */
async function start(plan: AuditionPlan, from?: number): Promise<void> {
  const my = ++token;
  silence();
  if (usePlayer.getState().currentId) player.stop();
  const m0 = Math.max(0, from ?? plan.out - plan.preRollS);
  useAudition.setState({ key: plan.key, status: "loading", mix: m0 });
  const [a, b] = deck();
  try {
    const [aSrc, bSrc] = await Promise.all([resolveMediaUrl(plan.aUrl), resolveMediaUrl(plan.bUrl)]);
    if (my !== token) return;
    if (a.dataset.src !== aSrc) {
      a.src = aSrc;
      a.dataset.src = aSrc;
    }
    if (b.dataset.src !== bSrc) {
      b.src = bSrc;
      b.dataset.src = bSrc;
    }
    b.playbackRate = plan.rate;
    b.preservesPitch = true;
    const outEnd = plan.out + plan.overlapS;
    const aNeeded = m0 < (plan.overlapS > 0 ? outEnd : plan.out);
    const bFrom = plan.inStart + Math.max(0, m0 - plan.out) * plan.rate;
    await Promise.all([aNeeded ? seekTo(a, m0) : Promise.resolve(), seekTo(b, bFrom)]);
    if (my !== token) return;
    const vol = () => usePlayer.getState().volume;
    let bOn = m0 >= plan.out;
    a.volume = vol();
    b.volume = bOn ? vol() : 0;
    if (aNeeded) await a.play();
    if (bOn) await b.play();
    if (my !== token) return;

    const tick = () => {
      if (my !== token) return;
      let m: number;
      if (!bOn) {
        m = a.currentTime;
        if (m >= plan.out - 0.008) {
          bOn = true;
          void b.play();
        }
      } else {
        m = plan.out + (b.currentTime - plan.inStart) / plan.rate;
      }
      const v = vol();
      if (plan.overlapS > 0) {
        const f = clamp01((m - plan.out) / plan.overlapS);
        b.volume = bOn ? v * clamp01(f * 2) : 0;
        a.volume = v * clamp01(2 - 2 * f);
        if (m >= outEnd && !a.paused) a.pause();
      } else {
        b.volume = bOn ? v : 0;
        if (bOn && !a.paused) a.pause();
      }
      if (m >= outEnd + plan.tailS || (b.ended && bOn) || (a.ended && !bOn)) {
        stop();
        return;
      }
      useAudition.setState({ status: "playing", mix: m });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
  } catch (e) {
    if (my !== token) return;
    stop();
    toast.error((e as Error).message, { title: "Audition failed", key: "audition" });
  }
}

export const audition = {
  start,
  stop,
  toggle(plan: AuditionPlan): void {
    const s = useAudition.getState();
    if (s.key === plan.key && s.status !== "idle") stop();
    else void start(plan);
  },
  isActive: (key: string) => {
    const s = useAudition.getState();
    return s.key === key && s.status !== "idle";
  },
};

// the global player and the audition never sound together
usePlayer.subscribe((s, prev) => {
  if ((s.status === "playing" || s.status === "loading") && s.status !== prev.status && useAudition.getState().status !== "idle") stop();
});
