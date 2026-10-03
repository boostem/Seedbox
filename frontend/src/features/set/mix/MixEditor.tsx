/**
 * The mix editor for one transition (chill mixes): style (ranked for the two songs) / bass swap / bars (re-generate),
 * the vocal-stems state, Edited badge, Save / Revert, undo/redo, the rendered-preview transport (render progress,
 * loop, LUFS/peak/warnings), the automation DAW, and (only with `details`) the plan's notes + markers. Space
 * plays/stops the rendered transition.
 */
import { AlertTriangle, AudioLines, Play, Redo2, Repeat, RotateCcw, Save, Sparkles, Square, Undo2 } from "lucide-react";
import { useCallback, useMemo } from "react";
import type { MixPlan, MixStyle, SetTrack, SwapKind, TrackAnalysis } from "@/api/types";
import { Badge, Button, IconButton, ProgressBar, Select, Spinner, Tabs, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useHotkey } from "@/lib/hotkeys";
import { type StemEntry, useApp, useStemState } from "@/store/app";
import { barLabel, BAR_OPTIONS, CHILL_STYLES, fitLabel, isChillStyle, STYLE_HINTS, SWAP_OPTIONS, SWAP_STYLES, usesFx } from "./lanes";
import { MixDaw } from "./MixDaw";
import { usePlayRequests } from "./pending";
import { markerColor } from "./paint";
import type { MixPlanApi } from "./useMixPlan";
import { useMixRender } from "./useMixRender";

export interface MixEditorProps {
  folder: string;
  a: SetTrack;
  b: SetTrack;
  anA: TrackAnalysis | null;
  anB: TrackAnalysis | null;
  mp: MixPlanApi & { plan: MixPlan };
  /** Show the plan notes + marker list (hidden until the user clicks Details). */
  details?: boolean;
}

/** The styles that are blends (they keep their length when you switch between them). */
const BLENDS: readonly MixStyle[] = ["phrase mix", "filter fade", "acapella"];

/** A set track's crate folder (from its audio URL: /api/files/<folder>/<file>), for crate-to-crate links too. */
function crateOf(t: SetTrack): string {
  const parts = t.audio_url.split("/");
  return parts.length > 3 ? decodeURIComponent(parts[3]) : "";
}

export function MixEditor({ folder, a, b, anA, anB, mp, details }: MixEditorProps) {
  const plan = mp.plan;
  const r = useMixRender(folder, plan, a, b);
  useHotkey("Space", () => r.toggle());
  const playFrom = useCallback((beat: number) => void r.play(beat), [r]);
  const { toggle } = r;
  usePlayRequests(`${a.track.id}>${b.track.id}`, true, toggle);

  const rendering = r.status === "rendering";
  const fits = useMemo(() => new Map((plan.style_fit ?? []).map((f) => [f.style, f])), [plan.style_fit]);
  // the styles ranked for these two songs (best fit first), and the strong alternatives as one-click chips
  const styles = useMemo(() => [...CHILL_STYLES].sort((x, y) => (fits.get(y.id)?.fit ?? -1) - (fits.get(x.id)?.fit ?? -1)), [fits]);
  const alsoFits = (plan.style_fit ?? []).filter((f) => f.style !== plan.style && f.fit >= 0.58 && isChillStyle(f.style)).slice(0, 3);
  const current = fits.get(plan.style);
  const styleTip = `${current?.why ? `Why ${plan.style}: ${current.why}. ` : ""}${STYLE_HINTS[plan.style] ?? "How the transition sounds"}`;
  const blend = BLENDS.includes(plan.style);
  const pick = (style: MixStyle) => void mp.regenerate(undefined, BLENDS.includes(style) && blend ? plan.bars : undefined, style);
  const swap = SWAP_OPTIONS.find((o) => o.id === plan.swap_kind);
  const swapTip = swap ? swap.hint : "B brings no bass during this blend, so A's lows carry it and leave with A. Pick a swap to force one.";
  const stA = useStemState(crateOf(a), a.track.id);
  const stB = useStemState(crateOf(b), b.track.id);
  const fx = usesFx(plan);
  return (
    <div className="flex flex-col gap-3">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2 rounded-md bg-surface px-3 py-2">
        <Button variant="primary" icon={r.playing ? Square : Play} loading={rendering && !r.playing} kbd="Space" onClick={r.toggle}>
          {r.playing ? "Stop" : "Play transition"}
        </Button>
        <IconButton icon={Repeat} label={r.loop ? "Loop playback: on" : "Loop playback: off"} active={r.loop} onClick={() => r.setLoop(!r.loop)} />
        <div className="flex min-w-[120px] flex-col gap-0.5">
          {rendering ? (
            <>
              <span className="flex items-center gap-1.5 text-2xs text-fg-muted">
                <Spinner size={10} /> Rendering{r.progress > 0 ? ` ${Math.round(r.progress * 100)}%` : "…"}
              </span>
              <ProgressBar value={r.progress > 0 ? r.progress : null} size="xs" />
            </>
          ) : r.status === "error" ? (
            <Tooltip content={r.error ?? "Render failed"}>
              <span className="max-w-[220px] truncate text-2xs text-danger select-text">{r.error ?? "Render failed"}</span>
            </Tooltip>
          ) : r.render ? (
            <span className={cn("text-2xs num", r.fresh ? "text-fg-subtle" : "text-warn")}>
              {r.fresh ? `${r.render.lufs.toFixed(1)} LUFS · ${r.render.true_peak_db.toFixed(1)} dBTP` : "edited: re-renders on play"}
            </span>
          ) : null}
        </div>
        {r.fresh && r.render && r.render.warnings.length > 0 && (
          <Tooltip content={r.render.warnings.join(" · ")}>
            <span className="inline-flex items-center gap-1 text-2xs text-warn">
              <AlertTriangle size={12} />
              {r.render.warnings.length}
            </span>
          </Tooltip>
        )}
        <Tooltip content={styleTip}>
          <Select value={isChillStyle(plan.style) ? plan.style : "phrase mix"} onChange={(e) => pick(e.target.value as MixStyle)} aria-label="Style" className="capitalize">
            {styles.map((st) => {
              const label = fitLabel(st.id, fits.get(st.id)?.fit);
              return (
                <option key={st.id} value={st.id}>
                  {label ? `${st.id} · ${label}` : st.id}
                </option>
              );
            })}
          </Select>
        </Tooltip>
        {SWAP_STYLES.includes(plan.style) && (
          <Tooltip content={swapTip}>
            <Select
              value={plan.swap_kind ?? "auto"}
              onChange={(e) => void mp.regenerate(undefined, plan.bars, plan.style, e.target.value === "auto" ? undefined : (e.target.value as SwapKind))}
              aria-label="Bass swap"
            >
              <option value="auto">{plan.swap_kind ? "Swap: pick for the songs" : "A carries the bass"}</option>
              {SWAP_OPTIONS.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </Select>
          </Tooltip>
        )}
        {blend && (
          <Tabs
            variant="segmented"
            value={String(plan.bars)}
            onChange={(v) => void mp.regenerate(undefined, Number(v), plan.style)}
            items={BAR_OPTIONS.map((n) => ({ id: String(n), label: `${n} bars` }))}
          />
        )}
        <StemsBadge plan={plan} a={stA} b={stB} canRefresh={!plan.edited && !mp.dirty} onRefresh={mp.reload} />
        <span className="flex-1" />
        {mp.dirty ? <Badge tone="warn">Unsaved edits</Badge> : plan.edited ? <Badge tone="accent">Edited</Badge> : null}
        <IconButton icon={Undo2} label="Undo (⌘Z)" size="md" onClick={mp.undo} disabled={!mp.canUndo} />
        <IconButton icon={Redo2} label="Redo (⇧⌘Z)" size="md" onClick={mp.redo} disabled={!mp.canRedo} />
        <Button variant="ghost" icon={RotateCcw} disabled={mp.saving || (!plan.edited && !mp.dirty)} onClick={() => void mp.revert()}>
          Revert
        </Button>
        <Button variant={mp.dirty ? "secondary" : "ghost"} icon={Save} loading={mp.saving} disabled={!mp.dirty} onClick={() => void mp.save()}>
          Save
        </Button>
      </div>

      {fx && (
        <div className="-mt-1.5 flex items-center gap-1.5 px-1 text-2xs text-warn">
          <AlertTriangle size={11} />
          This transition was saved with echo or reverb from the FX version. Revert to make it a chill mix.
        </div>
      )}

      {/* other styles that suit these two songs, one click away */}
      {alsoFits.length > 0 && (
        <div className="-mt-1.5 flex flex-wrap items-center gap-1.5 px-1 text-2xs text-fg-subtle">
          <Sparkles size={11} className="text-accent" />
          <span>Also fits these two:</span>
          {alsoFits.map((f) => (
            <Tooltip key={f.style} content={`${f.why}. ${STYLE_HINTS[f.style] ?? ""}`}>
              <button
                type="button"
                onClick={() => pick(f.style)}
                className="rounded-xs border border-line bg-raised px-1.5 py-px font-medium text-fg-muted capitalize transition-colors hover:border-accent/50 hover:text-fg"
              >
                {f.style}
              </button>
            </Tooltip>
          ))}
        </div>
      )}

      <MixDaw
        plan={plan}
        update={mp.update}
        checkpoint={mp.checkpoint}
        undo={mp.undo}
        redo={mp.redo}
        canUndo={mp.canUndo}
        canRedo={mp.canRedo}
        anA={anA}
        anB={anB}
        nameA={a.track.display_name}
        nameB={b.track.display_name}
        clock={r.clock}
        onPlayFrom={playFrom}
      />

      {/* plan notes + markers (on demand) */}
      {details && (
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <section className="rounded-md border border-line bg-surface px-3 py-2.5">
          <h3 className="label-caps mb-1.5 !text-fg-muted">
            The plan · <span className="capitalize">{blend ? `${plan.bars}-bar ${plan.style}` : plan.style}</span>
          </h3>
          <ul className="flex flex-col gap-1 text-sm select-text">
            {plan.notes.map((n, i) => (
              <li key={i} className={cn("flex gap-2", i === 0 ? "font-medium text-fg" : "text-fg-muted")}>
                <span className="mt-[7px] size-1 shrink-0 rounded-full bg-fg-faint" />
                {n}
              </li>
            ))}
          </ul>
        </section>
        <section className="rounded-md border border-line bg-surface px-1.5 py-2">
          <h3 className="label-caps mb-1 px-1.5 !text-fg-muted">Markers</h3>
          {plan.markers.map((m, i) => (
            <button key={i} type="button" onClick={() => playFrom(m.beat - 4)} className="flex h-7 w-full items-center gap-2 rounded-xs px-1.5 text-left hover:bg-raised" title="Play from a bar before">
              <span className="size-2 shrink-0 rotate-45 rounded-[1px]" style={{ background: markerColor(m.label) }} />
              <span className="min-w-0 flex-1 truncate text-sm text-fg capitalize">{m.label}</span>
              <span className="text-xs text-fg-subtle num">bar {barLabel(m.beat)}</span>
            </button>
          ))}
        </section>
      </div>
      )}
    </div>
  );
}

/** Whose vocals were split for this plan, live splitting progress for the rest, and a refresh once they're ready. */
function StemsBadge({ plan, a, b, canRefresh, onRefresh }: { plan: MixPlan; a?: StemEntry; b?: StemEntry; canRefresh: boolean; onRefresh: () => void }) {
  const split = plan.vocal_stems ?? { a: false, b: false };
  const available = useApp((s) => Object.values(s.stemsAvailable).every((x) => x.available));
  const reason = useApp((s) => Object.values(s.stemsAvailable).find((x) => !x.available)?.reason ?? null);
  const waiting = useApp((s) => s.stemModelWaiting);
  if (split.a && split.b) {
    return (
      <Tooltip content="Both songs' vocals are split: two vocals never sound at once, and acapellas and vocal-free loops are possible.">
        <span className="inline-flex items-center gap-1 text-2xs text-ok">
          <AudioLines size={12} /> Vocals split
        </span>
      </Tooltip>
    );
  }
  if (!available) {
    return (
      <Tooltip content={reason ?? "Vocal splitting isn't available on this install."}>
        <span className="inline-flex items-center gap-1 text-2xs text-fg-subtle">
          <AudioLines size={12} /> No vocal stems
        </span>
      </Tooltip>
    );
  }
  if (waiting) {
    return (
      <Tooltip content={`${waiting} Until then vocal clashes are handled with the EQ.`}>
        <span className="inline-flex items-center gap-1 text-2xs text-warn">
          <AudioLines size={12} /> Vocal model: retrying
        </span>
      </Tooltip>
    );
  }
  const live = (["a", "b"] as const).filter((d) => !split[d]).map((d) => ({ d, st: d === "a" ? a : b }));
  const ready = live.filter((x) => x.st?.state === "ready");
  if (ready.length === live.length && live.length > 0) {
    return (
      <Tooltip content={canRefresh ? "The vocals are split now: update the plan to use them." : "The vocals are split now: Revert to a generated plan to use them."}>
        <Button variant="ghost" size="sm" icon={AudioLines} disabled={!canRefresh} onClick={onRefresh}>
          Vocals ready: update
        </Button>
      </Tooltip>
    );
  }
  const running = live.find((x) => x.st?.state === "running");
  const failed = live.find((x) => x.st?.state === "error");
  const text = running ? `Splitting vocals ${Math.round((running.st?.progress ?? 0) * 100)}%` : failed ? "Vocal split failed" : "Vocals not split yet";
  const who = live.map((x) => x.d.toUpperCase()).join(" and ");
  return (
    <Tooltip content={failed?.st?.error ? `Splitting ${failed.d.toUpperCase()}'s vocals failed: ${failed.st.error}` : `${who}'s vocals are split in the background (songs you're using go first). Until then vocal clashes are handled with the EQ.`}>
      <span className={cn("inline-flex items-center gap-1 text-2xs", failed ? "text-warn" : "text-fg-subtle")}>
        {running ? <Spinner size={10} /> : <AudioLines size={12} />} {text}
      </span>
    </Tooltip>
  );
}
