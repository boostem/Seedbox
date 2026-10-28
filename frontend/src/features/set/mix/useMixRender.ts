/**
 * Hearing a MixPlan: POST …/render (with the current, possibly unsaved plan) → MixRender, progress from the
 * `render` SSE event, playback through the GLOBAL player (one sound at a time, mini player shows it). The playhead
 * is mapped audio time → master beat through beat_times_s. While the transition is playing, edits re-render after
 * 600 ms and playback resumes at the same master beat. Optional loop playback restarts at the window start.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { MixPlan, MixRender, SetTrack } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { toast } from "@/store/toasts";
import { beatAt, timeAt } from "./lanes";

export interface BeatClock {
  /** Current master beat while this transition plays (or null). */
  get: () => number | null;
  subscribe: (cb: () => void) => () => void;
}

export interface MixRenderApi {
  render: MixRender | null;
  /** The render matches the current plan. */
  fresh: boolean;
  status: "idle" | "rendering" | "error";
  progress: number;
  error: string | null;
  playing: boolean;
  loop: boolean;
  setLoop: (v: boolean) => void;
  play: (fromBeat?: number) => Promise<void>;
  stop: () => void;
  toggle: () => void;
  clock: BeatClock;
  playerId: string;
}

const RERENDER_MS = 600;

export function useMixRender(folder: string, plan: MixPlan | null, a: SetTrack, b: SetTrack): MixRenderApi {
  const playerId = `mix:${a.track.id}>${b.track.id}`;
  const [render, setRender] = useState<MixRender | null>(null);
  const [renderedFor, setRenderedFor] = useState("");
  const [status, setStatus] = useState<MixRenderApi["status"]>("idle");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loop, setLoop] = useState(false);
  const playing = usePlayer((s) => s.currentId === playerId && (s.status === "playing" || s.status === "loading"));
  const seq = useRef(0);
  const renderRef = useRef(render);
  renderRef.current = render;
  const planRef = useRef(plan);
  planRef.current = plan;
  const loopRef = useRef(loop);
  loopRef.current = loop;
  const key = useMemo(() => (plan ? JSON.stringify({ ...plan, notes: [], markers: [], edited: false }) : ""), [plan]);
  const keyRef = useRef(key);
  keyRef.current = key;

  // progress events for this pair
  useEffect(
    () =>
      onServerEvent((ev) => {
        if (ev.type !== "render") return;
        const d = ev.data;
        if (d.folder !== folder || d.from_id !== a.track.id || d.to_id !== b.track.id) return;
        if (d.state === "running") setProgress(d.progress);
      }),
    [folder, a.track.id, b.track.id],
  );

  const track = useMemo(() => ({ ...b.track, id: playerId, name: `${a.track.name} → ${b.track.name}`, artists: ["Transition preview"] }), [a.track, b.track, playerId]);

  const ensure = useCallback(async (): Promise<MixRender | null> => {
    const p = planRef.current;
    if (!p) return null;
    const k = keyRef.current;
    if (renderRef.current && renderedFor === k) return renderRef.current;
    const my = ++seq.current;
    setStatus("rendering");
    setProgress(0);
    setError(null);
    try {
      const r = await api.renderMix(folder, a.track.id, b.track.id, p, { quiet: true });
      if (my !== seq.current) return null;
      setRender(r);
      renderRef.current = r;
      setRenderedFor(k);
      setStatus("idle");
      return r;
    } catch (e) {
      if (my !== seq.current) return null;
      setStatus("error");
      setError((e as Error).message);
      toast.error((e as Error).message, { title: "Render failed", key: "mixrender" });
      return null;
    }
  }, [folder, a.track.id, b.track.id, renderedFor]);

  const play = useCallback(
    async (fromBeat?: number) => {
      const r = await ensure();
      if (!r) return;
      const startAt = fromBeat != null ? Math.max(0, Math.min(r.duration_s - 0.05, timeAt(r, fromBeat))) : 0;
      const st = usePlayer.getState();
      if (st.currentId === playerId && st.url === r.audio_url && (st.status === "playing" || st.status === "paused")) {
        player.seek(startAt);
        if (st.status === "paused") player.resume();
        return;
      }
      await player.play(playerId, r.audio_url, { track, source: "file", startAt });
    },
    [ensure, playerId, track],
  );

  const stop = useCallback(() => {
    if (usePlayer.getState().currentId === playerId) player.stop();
  }, [playerId]);

  const toggle = useCallback(() => {
    if (playing) stop();
    else void play();
  }, [playing, play, stop]);

  // re-render after edits while playing, then continue at the same master beat
  useEffect(() => {
    if (!plan || !render || key === renderedFor || !playing) return;
    const t = setTimeout(async () => {
      const r0 = renderRef.current;
      const r = await ensure();
      const st = usePlayer.getState();
      if (!r || !r0 || st.currentId !== playerId) return;
      // the old render kept playing while the new one was made: continue from where it is now
      const beat = beatAt(r0, st.position);
      await player.play(playerId, r.audio_url, { track, source: "file", startAt: Math.max(0, Math.min(r.duration_s - 0.05, timeAt(r, beat))) });
    }, RERENDER_MS);
    return () => clearTimeout(t);
  }, [key, renderedFor, plan, render, playing, ensure, playerId, track]);

  // loop playback
  useEffect(
    () =>
      usePlayer.subscribe((s, prev) => {
        if (!loopRef.current || s.currentId !== playerId) return;
        if (prev.status === "playing" && s.status === "idle" && renderRef.current) {
          void player.play(playerId, renderRef.current.audio_url, { track, source: "file", startAt: 0 });
        }
      }),
    [playerId, track],
  );

  // stop this preview when the editor goes away
  useEffect(() => () => {
    if (usePlayer.getState().currentId === playerId) player.stop();
  }, [playerId]);

  const clock = useMemo<BeatClock>(
    () => ({
      get: () => {
        const s = usePlayer.getState();
        const r = renderRef.current;
        if (!r || s.currentId !== playerId || s.status === "idle" || s.status === "error" || s.url !== r.audio_url) return null;
        return beatAt(r, s.position);
      },
      subscribe: (cb) => usePlayer.subscribe(cb),
    }),
    [playerId],
  );

  return { render, fresh: !!render && renderedFor === key, status, progress, error, playing, loop, setLoop, play, stop, toggle, clock, playerId };
}
