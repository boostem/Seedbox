/**
 * The v4 mix editor for one transition: technique / style / bars (re-generate), Edited badge, Save / Revert, undo/redo,
 * the rendered-preview transport (render progress, loop, LUFS/peak/warnings), the automation DAW, and (only with
 * `details`) the plan's notes + markers. Space plays/stops the rendered transition.
 */
import { AlertTriangle, Play, Redo2, Repeat, RotateCcw, Save, Square, Undo2 } from "lucide-react";
import { useCallback } from "react";
import type { MixPlan, MixStyle, MixTechnique, SetTrack, TrackAnalysis } from "@/api/types";
import { Badge, Button, IconButton, ProgressBar, Select, Spinner, Tabs, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useHotkey } from "@/lib/hotkeys";
import { barLabel, BAR_OPTIONS, BLEND_STYLES, isBlend, TECHNIQUES } from "./lanes";
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

export function MixEditor({ folder, a, b, anA, anB, mp, details }: MixEditorProps) {
  const plan = mp.plan;
  const r = useMixRender(folder, plan, a, b);
  useHotkey("Space", () => r.toggle());
  const playFrom = useCallback((beat: number) => void r.play(beat), [r]);
  const { toggle } = r;
  usePlayRequests(`${a.track.id}>${b.track.id}`, true, toggle);

  const rendering = r.status === "rendering";
  const blend = isBlend(plan.technique);
  const styleHint = BLEND_STYLES.find((s) => s.id === plan.style)?.hint;
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
        <Select value={plan.technique} onChange={(e) => void mp.regenerate(e.target.value as MixTechnique, plan.bars)} aria-label="Technique" className="capitalize">
          {TECHNIQUES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </Select>
        {blend && (
          <Tooltip content={styleHint ?? "How the blend sounds"}>
            <Select value={plan.style} onChange={(e) => void mp.regenerate(plan.technique, plan.bars, e.target.value as MixStyle)} aria-label="Style" className="capitalize">
              {BLEND_STYLES.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.id}
                </option>
              ))}
            </Select>
          </Tooltip>
        )}
        <Tabs
          variant="segmented"
          value={String(plan.bars)}
          onChange={(v) => void mp.regenerate(plan.technique, Number(v), blend ? plan.style : undefined)}
          items={BAR_OPTIONS.map((n) => ({ id: String(n), label: `${n} bars` }))}
        />
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
            The plan · <span className="capitalize">{plan.bars}-bar {plan.technique}</span>
            {blend && plan.style && <span className="capitalize"> · {plan.style}</span>}
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
