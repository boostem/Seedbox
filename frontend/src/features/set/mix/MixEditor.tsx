/**
 * The v4 mix editor for one transition: technique / bars (re-generate), Edited badge, Save / Revert, undo/redo,
 * the rendered-preview transport (render progress, loop, LUFS/peak/warnings), the automation DAW, and (only with
 * `details`) the plan's notes + markers. Space plays/stops the rendered transition.
 *
 * The view (settings.mix_view, switchable here): "simple" = style, length and amounts over a read-only overview;
 * "advanced" = the main lanes, the filter's resonance following it; "manual" = every lane.
 */
import { AlertTriangle, Play, Redo2, Repeat, RotateCcw, Save, Square, Undo2 } from "lucide-react";
import { useCallback, useRef } from "react";
import { api } from "@/api/client";
import type { MixPlan, MixTechnique, MixView, SetTrack, TrackAnalysis } from "@/api/types";
import { Badge, Button, IconButton, ProgressBar, Select, Slider, Spinner, Tabs, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useHotkey } from "@/lib/hotkeys";
import { useApp } from "@/store/app";
import { barLabel, BAR_OPTIONS, TECHNIQUES } from "./lanes";
import { MixDaw } from "./MixDaw";
import { usePlayRequests } from "./pending";
import { markerColor } from "./paint";
import type { MixPlanApi } from "./useMixPlan";
import { useMixRender } from "./useMixRender";
import { AMOUNTS, amountOf, followFilter, MIN_AMOUNT, MIX_VIEWS, setAmount, STYLE_HINTS } from "./views";

/** The editor view from settings; switching it saves the setting (the settings page shows the same choice). */
function useMixView(): [MixView, (v: MixView) => void] {
  const view = useApp((s) => s.settings?.mix_view ?? "simple");
  const setSettings = useApp((s) => s.setSettings);
  const set = useCallback(
    (v: MixView) => {
      const cur = useApp.getState().settings;
      if (cur) setSettings({ ...cur, mix_view: v }); // switch right away, the server copy follows
      void api.saveSettings({ mix_view: v }).then(setSettings, () => cur && setSettings(cur));
    },
    [setSettings],
  );
  return [view, set];
}

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

  const [view, setView] = useMixView();
  // advanced: an edit to a filter lane re-draws that deck's resonance to follow it
  const planRef = useRef(plan);
  planRef.current = plan;
  const { update } = mp;
  const advancedUpdate = useCallback(
    (p: MixPlan, opts?: { transient?: boolean }) => {
      const next = followFilter(planRef.current, p);
      planRef.current = next;
      update(next, opts);
    },
    [update],
  );

  const rendering = r.status === "rendering";
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
              {r.fresh ? `${r.render.lufs.toFixed(1)} LUFS · ${r.render.true_peak_db.toFixed(1)} dBTP` : "edited — re-renders on play"}
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
        <Tabs variant="segmented" value={String(plan.bars)} onChange={(v) => void mp.regenerate(plan.technique, Number(v))} items={BAR_OPTIONS.map((n) => ({ id: String(n), label: `${n} bars` }))} />
        <span className="flex-1" />
        <Tabs variant="segmented" value={view} onChange={(v) => setView(v as MixView)} items={MIX_VIEWS.map(({ id, label, hint }) => ({ id, label, title: hint }))} />
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

      {view === "simple" && <SimplePanel plan={plan} mp={mp} />}

      <MixDaw
        view={view}
        plan={plan}
        update={view === "advanced" ? advancedUpdate : mp.update}
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

/** Simple view: what the style does (the plan's headline) and an amount slider per effect the style uses. */
function SimplePanel({ plan, mp }: { plan: MixPlan; mp: MixPlanApi }) {
  const start = useRef<MixPlan | null>(null); // the plan before a slider drag (one undo step per drag)
  const planRef = useRef(plan);
  planRef.current = plan;
  const change = (id: (typeof AMOUNTS)[number]["id"], v: number) => {
    start.current ??= planRef.current;
    mp.update(setAmount(planRef.current, id, v), { transient: true });
  };
  const commit = () => {
    if (start.current) mp.checkpoint(start.current);
    start.current = null;
  };
  return (
    <section className="flex flex-wrap items-start gap-x-8 gap-y-3 rounded-md border border-line bg-surface px-4 py-3">
      <div className="min-w-[240px] flex-1">
        <h3 className="label-caps mb-1 !text-fg-muted">
          <span className="capitalize">{plan.technique}</span> · {plan.bars} bars
        </h3>
        <p className="text-sm text-fg select-text">{STYLE_HINTS[plan.technique]}</p>
        {plan.notes[0] && <p className="mt-1 text-xs text-fg-subtle select-text">{plan.notes[0]}</p>}
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        {AMOUNTS.map((m) => {
          const v = amountOf(plan, m.id);
          const used = v >= 1e-3;
          return (
            <Tooltip key={m.id} content={used ? m.hint : `This style doesn't use the ${m.label.toLowerCase()}`}>
              <label className={cn("flex w-36 flex-col gap-1", !used && "opacity-50")}>
                <span className="flex items-center justify-between text-xs text-fg-muted">
                  {m.label}
                  <span className="text-2xs text-fg-subtle num">{used ? `${Math.round(v * 100)}%` : "off"}</span>
                </span>
                <Slider label={`${m.label} amount`} value={used ? v : 0} min={MIN_AMOUNT} max={1} step={0.01} disabled={!used} onChange={(x) => change(m.id, x)} onCommit={commit} />
              </label>
            </Tooltip>
          );
        })}
      </div>
    </section>
  );
}
