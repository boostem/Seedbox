/**
 * Full set mix: render the whole set as one continuous mix (every transition's plan back to back, set-wide tempo
 * continuity), follow the render, play it through the global player with a clickable tracklist, reveal the files.
 * `SetMixButton` is the page-header entry point (shows render progress) that opens the panel in a modal.
 */
import { AlertTriangle, Check, Disc3, FolderOpen, Play, RefreshCw, Square, X } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import type { SetMix, SetMixResult } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { Badge, Button, IconButton, KeyBadge, Modal, ProgressBar, Spinner, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtRelative, fmtTime, plural, tildify } from "@/lib/format";
import { setMixTrack } from "@/store/app";
import { useSetMix } from "./useSetMix";

const playerId = (m: SetMix) => `setmix:${m.mix_id}`;

/** Play the mix from `at` seconds (seek when it's already loaded). */
export function playSetMix(m: SetMix, at = 0): void {
  if (!m.result) return;
  const st = usePlayer.getState();
  if (st.currentId === playerId(m) && (st.status === "playing" || st.status === "paused" || st.status === "loading")) {
    player.seek(at);
    if (st.status === "paused") player.resume();
    return;
  }
  void player.play(playerId(m), m.result.audio_url, { track: setMixTrack(m), source: "file", startAt: at });
}

function Tracklist({ m, r }: { m: SetMix; r: SetMixResult }) {
  const id = playerId(m);
  const pos = usePlayer((s) => (s.currentId === id && s.status !== "idle" ? s.position : null));
  const current = pos == null ? -1 : r.tracks.reduce((cur, t, i) => (pos >= t.start_s - 0.25 ? i : cur), 0);
  const how = useMemo(() => new Map(r.transitions.map((t) => [t.index + 1, t])), [r.transitions]);
  return (
    <ol className="flex max-h-[340px] flex-col overflow-y-auto scroll-thin rounded-md border border-line bg-bg/40 py-1">
      {r.tracks.map((t, i) => {
        const tr = how.get(i);
        const on = i === current;
        return (
          <li key={`${t.track_id}-${i}`}>
            <button
              type="button"
              onClick={() => playSetMix(m, Math.max(0, t.start_s - (i ? 2 : 0)))}
              className={cn("group flex h-9 w-full items-center gap-2.5 px-3 text-left transition-colors hover:bg-raised", on && "bg-accent/12")}
              title="Play the mix from here"
            >
              <span className={cn("w-12 shrink-0 text-xs num", on ? "text-accent-hover" : "text-fg-subtle")}>{fmtTime(t.start_s)}</span>
              <span className="w-5 shrink-0 text-right text-2xs text-fg-faint num">{i + 1}</span>
              <span className="min-w-0 flex-1">
                <span className={cn("block truncate text-sm", on ? "font-semibold text-fg" : "text-fg")}>{t.name}</span>
                <span className="block truncate text-2xs text-fg-subtle">{t.artists.join(", ")}</span>
              </span>
              {tr && (
                <Tooltip content={`${tr.technique}${tr.overlap ? ` · ${tr.overlap}-bar blend` : ""}${tr.edited ? " · your saved edit" : ""}`}>
                  <span className="hidden shrink-0 items-center gap-1 rounded-xs bg-raised px-1.5 py-0.5 text-2xs text-fg-muted capitalize @lg:inline-flex">
                    {tr.technique}
                    {tr.overlap ? <span className="num text-fg-subtle">{tr.overlap}</span> : null}
                    {tr.edited && <span className="size-1.5 rounded-full bg-accent" />}
                  </span>
                </Tooltip>
              )}
              <KeyBadge camelot={t.camelot} size="xs" />
              <span className="w-9 shrink-0 text-right text-2xs text-fg-subtle num">{fmtBpm(t.bpm)}</span>
              <Play size={12} className="shrink-0 text-fg-faint opacity-0 group-hover:opacity-100" />
            </button>
          </li>
        );
      })}
    </ol>
  );
}

export interface SetMixPanelProps {
  target: string;
  trackCount: number;
  /** Bumps when the set changes (refetches the stale flag). */
  refreshKey?: unknown;
}

export function SetMixPanel({ target, trackCount, refreshKey }: SetMixPanelProps) {
  const sm = useSetMix(target, refreshKey);
  const m = sm.mix;
  const r = m?.state === "done" ? m.result : null;
  const playing = usePlayer((s) => !!m && s.currentId === playerId(m) && (s.status === "playing" || s.status === "loading"));
  const canRender = trackCount >= 2;

  return (
    <div className="@container flex flex-col gap-3">
      <p className="text-sm text-fg-muted">
        Every transition of this set, exactly as planned (your saved edits included), mixed back to back into one continuous MP3 with a tracklist
        (<span className="text-fg">.cue</span> + <span className="text-fg">.txt</span>, chapters in the file). Tempo moves are spread across each track so the
        whole set flows; loops, echo throws, washes and turntable FX all render.
      </p>

      {sm.busy && m ? (
        <div className="flex flex-col gap-2 rounded-md border border-accent/30 bg-accent/8 px-3 py-2.5">
          <div className="flex items-center gap-2">
            <Spinner size={13} className="text-accent-hover" />
            <span className="text-sm font-medium text-fg capitalize">{m.state === "queued" ? "Queued" : m.state === "preparing" ? "Preparing" : "Rendering"}</span>
            <span className="text-xs text-fg-subtle num">{Math.round(m.progress * 100)}%</span>
            <span className="flex-1" />
            <Button size="sm" variant="ghost" icon={X} onClick={() => void sm.cancel()}>
              Cancel
            </Button>
          </div>
          <ProgressBar value={m.state === "queued" ? null : Math.max(0.01, m.progress)} size="sm" />
          <span className="truncate text-xs text-fg-subtle select-text">{m.message}</span>
        </div>
      ) : r && m ? (
        <div className="flex flex-col gap-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" icon={playing ? Square : Play} onClick={() => (playing ? player.stop() : playSetMix(m))}>
              {playing ? "Stop" : "Play mix"}
            </Button>
            <div className="flex flex-col">
              <span className="text-sm font-semibold text-fg num">
                {fmtTime(r.duration_s)} · {plural(r.tracks.length, "track")}
              </span>
              <span className="text-2xs text-fg-subtle num">
                {r.format.toUpperCase()} · {r.lufs.toFixed(1)} LUFS · {r.true_peak_db.toFixed(1)} dBTP · {fmtRelative(m.finished_at)}
              </span>
            </div>
            <span className="flex-1" />
            {sm.stale ? (
              <Tooltip content="The running order or a transition changed since this mix was rendered">
                <span>
                  <Badge tone="warn">Set changed</Badge>
                </span>
              </Tooltip>
            ) : (
              <Badge tone="ok" icon={Check}>
                Up to date
              </Badge>
            )}
            <IconButton icon={FolderOpen} label="Reveal the mix in Finder" onClick={() => void api.reveal(r.path)} />
            <Button variant={sm.stale ? "secondary" : "ghost"} icon={RefreshCw} loading={sm.starting} disabled={!canRender} onClick={() => void sm.start()}>
              Re-render
            </Button>
          </div>
          <Tracklist m={m} r={r} />
          <span className="truncate text-2xs text-fg-faint select-text" title={r.path}>
            {tildify(r.path)}
          </span>
          {r.warnings.length > 0 && (
            <ul className="flex flex-col gap-1 text-xs text-warn select-text">
              {r.warnings.map((w, i) => (
                <li key={i} className="flex gap-1.5">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                  {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="flex flex-col items-start gap-2.5">
          {m?.state === "error" && (
            <div className="flex w-full gap-2 rounded-md border border-danger/30 bg-danger/8 px-3 py-2 text-sm text-danger select-text">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span>The last render failed: {m.error}</span>
            </div>
          )}
          {m?.state === "cancelled" && <span className="text-xs text-fg-subtle">The last render was cancelled.</span>}
          <Button variant="primary" icon={Disc3} loading={sm.starting || sm.loading} disabled={!canRender} onClick={() => void sm.start()}>
            {m?.state === "error" ? "Try again" : "Render the full mix"}
          </Button>
          {!canRender && <span className="text-xs text-fg-subtle">Needs at least two tracks.</span>}
          {canRender && (
            <span className="text-xs text-fg-subtle">
              Takes about a minute per 10 minutes of music. Missing analyses run first; you can keep working meanwhile.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** Header button: "Full mix" (with live render progress) → the panel in a modal. */
export function SetMixButton({ target, title, trackCount, refreshKey }: SetMixPanelProps & { title: string }) {
  const [open, setOpen] = useState(false);
  const sm = useSetMix(target, refreshKey);
  const m = sm.mix;
  return (
    <>
      <Button variant="secondary" icon={sm.busy ? undefined : Disc3} onClick={() => setOpen(true)} disabled={trackCount < 2} className={cn(sm.busy && "min-w-[132px]")}>
        {sm.busy && m ? (
          <span className="inline-flex items-center gap-1.5">
            <Spinner size={12} className="text-accent-hover" />
            <span className="num">{m.state === "queued" ? "Queued" : `Mixing ${Math.round(m.progress * 100)}%`}</span>
          </span>
        ) : m?.state === "done" && !sm.stale ? (
          "Full mix ✓"
        ) : (
          "Full mix"
        )}
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} size="lg" title={`Full mix · ${title}`}>
        <SetMixPanel target={target} trackCount={trackCount} refreshKey={refreshKey} />
      </Modal>
    </>
  );
}
