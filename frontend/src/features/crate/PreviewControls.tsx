/**
 * Store-driven preview UI for track rows: a play button that reflects the live preview state (queued / loading /
 * ready / no_match / error) and the player, plus the clip's mini waveform with a playhead. Both read only their
 * own track's slice of the stores, so a playing row re-renders alone.
 */
import { CircleSlash, Play, Square, TriangleAlert } from "lucide-react";
import { useEffect } from "react";
import { api } from "@/api/client";
import type { PreviewStatus, Track } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { PeaksWaveform, Spinner, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtTime } from "@/lib/format";
import { useApp, usePreview } from "@/store/app";

const CLIP_S = 20;

/** The player's status for this track (null when it isn't the current one). */
function usePlayerStatusFor(id: string) {
  return usePlayer((s) => (s.currentId === id ? s.status : null));
}

function stateHelp(p: PreviewStatus | undefined, status: ReturnType<typeof usePlayerStatusFor>): string {
  if (status === "playing") return "Stop preview (Space)";
  if (status === "loading") return "Fetching the clip: plays as soon as it's ready";
  switch (p?.state) {
    case "ready":
      return p.youtube_title ? `Play 20 s preview · ${p.youtube_title}` : "Play 20 s preview";
    case "loading":
      return "Preloading the clip… click to play when ready";
    case "queued":
      return "Queued for preloading: click to fetch it now";
    case "no_match":
      return p.error ? `No preview: ${p.error}` : "No YouTube match for a preview";
    case "error":
      return `Preview failed${p.error ? `: ${p.error}` : ""}. Click to retry`;
    default:
      return "Play 20 s preview (fetched on demand)";
  }
}

export interface PreviewButtonProps {
  track: Track;
  size?: "sm" | "md";
  className?: string;
}

/** Round play/stop button for a row. Click = player.togglePreview (prioritises + waits if not ready yet). */
export function PreviewButton({ track, size = "sm", className }: PreviewButtonProps) {
  const p = usePreview(track.id);
  const status = usePlayerStatusFor(track.id);
  const playing = status === "playing";
  const pending = status === "loading";
  const state = p?.state;
  const dim = size === "sm" ? "size-[22px]" : "size-8";
  const icon = size === "sm" ? 10 : 13;

  let body;
  let tone: string;
  if (playing) {
    body = <Square size={icon - 2} fill="currentColor" strokeWidth={0} />;
    tone = "bg-accent text-white hover:bg-accent-hover";
  } else if (pending) {
    body = <Spinner size={icon + 2} />;
    tone = "text-accent-hover ring-1 ring-accent/60 ring-inset";
  } else if (state === "loading") {
    body = <Spinner size={icon + 1} className="text-fg-subtle" />;
    tone = "text-fg-subtle ring-1 ring-line-strong ring-inset hover:text-fg";
  } else if (state === "no_match") {
    body = <CircleSlash size={icon + 2} strokeWidth={1.75} />;
    tone = "text-fg-faint hover:text-fg-subtle";
  } else if (state === "error") {
    body = <TriangleAlert size={icon + 2} strokeWidth={2} />;
    tone = "text-warn/90 hover:text-warn";
  } else if (state === "ready") {
    body = <Play size={icon} fill="currentColor" strokeWidth={0} className="ml-px" />;
    tone = "bg-raised text-fg ring-1 ring-line-strong ring-inset hover:bg-accent hover:text-white hover:ring-accent";
  } else {
    body = <Play size={icon} fill="currentColor" strokeWidth={0} className="ml-px" />;
    tone = "text-fg-subtle ring-1 ring-line ring-inset hover:text-fg hover:ring-line-strong";
  }

  return (
    <Tooltip content={stateHelp(p, status)} delay={500}>
      <button
        type="button"
        aria-label={playing ? `Stop ${track.display_name}` : `Play preview of ${track.display_name}`}
        data-preview-state={state ?? "idle"}
        onClick={() => player.togglePreview(track)}
        className={cn("grid shrink-0 place-items-center rounded-full transition-colors duration-100", dim, tone, className)}
      >
        {body}
      </button>
    </Tooltip>
  );
}

export interface PreviewWaveProps {
  track: Track;
  /** Bars to draw (resampled from ~200 peaks). */
  bars?: number;
  height?: number;
  /** Show the clip position (0:07) next to the waveform while playing. */
  showTime?: boolean;
  className?: string;
}

/** The clip's mini waveform (or its state label); click to play from that point / seek while playing. */
export function PreviewWave({ track, bars = 46, height = 18, showTime = true, className }: PreviewWaveProps) {
  const p = usePreview(track.id);
  const status = usePlayerStatusFor(track.id);
  const progress = usePlayer((s) => (s.currentId === track.id && s.duration ? s.position / s.duration : null));
  const position = usePlayer((s) => (s.currentId === track.id && showTime ? Math.floor(s.position) : null));
  const current = status === "playing" || status === "paused";

  if (p?.state === "ready" && p.peaks?.length) {
    return (
      <div className={cn("flex min-w-0 items-center gap-1.5", className)}>
        <PeaksWaveform
          peaks={p.peaks}
          bars={bars}
          height={height}
          progress={current ? progress : null}
          color={current ? "var(--color-fg-subtle)" : "var(--color-fg-faint)"}
          playedColor="var(--color-accent-hover)"
          className="min-w-0 flex-1"
          onSeek={(f) => {
            if (current) player.seekFraction(f);
            else if (p.audio_url) void player.play(track.id, p.audio_url, { track, source: "preview", startAt: f * CLIP_S });
          }}
        />
        {showTime && <span className={cn("w-7 shrink-0 text-right text-2xs num", current ? "text-fg-muted" : "text-fg-faint")}>{current ? fmtTime(position) : "0:20"}</span>}
      </div>
    );
  }

  const label = (() => {
    if (status === "loading") return { text: "Fetching…", cls: "text-accent-hover", spin: true };
    switch (p?.state) {
      case "loading":
        return { text: "Preloading…", cls: "text-fg-subtle", spin: true };
      case "queued":
        return { text: "Queued", cls: "text-fg-faint", spin: false };
      case "no_match":
        return { text: "No match", cls: "text-fg-subtle", spin: false };
      case "error":
        return { text: "Error · retry", cls: "text-warn", spin: false };
      case "ready":
        return { text: "Ready", cls: "text-fg-subtle", spin: false };
      default:
        return { text: "On demand", cls: "text-fg-faint", spin: false };
    }
  })();
  const tip = p?.state === "no_match" || p?.state === "error" ? p.error : null;
  const content = (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5 text-2xs", label.cls)}>
      {label.spin ? <Spinner size={10} /> : <span className={cn("h-px w-3 shrink-0", p?.state === "queued" ? "bg-line-strong" : "bg-current opacity-40")} />}
      <span className="truncate">{label.text}</span>
    </span>
  );
  return (
    <div className={cn("flex min-w-0 items-center", className)} style={{ height }}>
      {tip ? (
        <Tooltip content={<span className="select-text">{tip}</span>} className="min-w-0">
          {content}
        </Tooltip>
      ) : (
        content
      )}
    </div>
  );
}

export interface PreviewCounts {
  total: number;
  ready: number;
  busy: number; // queued + loading
  noMatch: number;
  error: number;
}

/** Live preview counts for a set of tracks (re-renders only when a count changes). */
export function usePreviewCounts(ids: string[]): PreviewCounts {
  const key = useApp((s) => {
    let ready = 0;
    let busy = 0;
    let noMatch = 0;
    let error = 0;
    for (const id of ids) {
      const st = s.previews[id]?.state;
      if (st === "ready") ready++;
      else if (st === "queued" || st === "loading") busy++;
      else if (st === "no_match") noMatch++;
      else if (st === "error") error++;
    }
    return `${ready}|${busy}|${noMatch}|${error}`;
  });
  const [ready, busy, noMatch, error] = key.split("|").map(Number);
  return { total: ids.length, ready, busy, noMatch, error };
}

const TERMINAL = new Set(["ready", "no_match", "error"]);

/**
 * Previews already in the server's disk cache don't produce `preview` events when a crate preloads them, so
 * fill the store for tracks it knows nothing about (and, after an SSE reconnect, re-check in-flight ones).
 */
export function useSyncPreviewStatuses(ids: string[], enabled: boolean): void {
  const epoch = useApp((s) => s.epoch);
  const key = ids.join(",");
  useEffect(() => {
    if (!enabled || !key) return;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      const store = useApp.getState();
      const todo = key.split(",").filter((id) => {
        const st = store.previews[id]?.state;
        return !st || st === "idle" || st === "queued" || st === "loading";
      });
      for (let i = 0; i < todo.length && !ctrl.signal.aborted; i += 6) {
        const batch = await Promise.allSettled(todo.slice(i, i + 6).map((id) => api.preview(id, { quiet: true, signal: ctrl.signal })));
        if (ctrl.signal.aborted) return;
        for (const r of batch) {
          if (r.status !== "fulfilled") continue;
          const fresh = r.value;
          const cur = useApp.getState().previews[fresh.track_id];
          // never let a slower poll overwrite a newer SSE update, except to fill gaps or land a terminal state
          if (!cur || cur.state === "idle" || (TERMINAL.has(fresh.state) && !TERMINAL.has(cur.state))) useApp.getState().upsertPreview(fresh);
        }
      }
    }, 700);
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [key, enabled, epoch]);
}
