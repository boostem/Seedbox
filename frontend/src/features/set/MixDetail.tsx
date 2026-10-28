import { AlertTriangle, ArrowRight, ChevronLeft, ChevronRight, Headphones, Info, Square } from "lucide-react";
import { useCallback, useMemo } from "react";
import type { SetTrack } from "@/api/types";
import { Button, CoverArt, IconButton, MoveChip, ScoreChip, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm } from "@/lib/format";
import { type MixClock, MixWaveform, mixGeometry } from "@/features/waveform";
import { type AuditionPlan, audition, useAudition } from "./audition";
import { PitchChip } from "./bits";
import { type SetTransition, techniqueLong } from "./model";
import { MixNotes } from "./MixNotes";
import { MixStats } from "./MixStats";
import { useTrackAnalysis } from "./useAnalysis";
import { useSessionState } from "./useSessionState";
import { MixEditor } from "./mix/MixEditor";
import { useMixPlan } from "./mix/useMixPlan";
import { usePlayRequests } from "./mix/pending";
import { mixPlaceholderView, WavePlaceholder } from "./WavePlaceholder";

export interface MixDetailProps {
  folder: string;
  t: SetTransition;
  a: SetTrack;
  b: SetTrack;
  index: number; // transition index (A = index, B = index + 1)
  count: number;
  onPrev?: () => void;
  onNext?: () => void;
  onSelectTrack: (id: string) => void;
}

/** Audition plan for a ready transition (needs A's bar length; falls back to the tunebat BPM). */
export function auditionPlan(t: SetTransition, a: SetTrack, b: SetTrack, barA?: number): AuditionPlan | null {
  if (!t.ready || t.out_start_s == null || t.in_start_s == null) return null;
  const bar = barA && barA > 0 ? barA : 240 / (a.track.bpm || 120);
  return {
    key: `${t.from_id}>${t.to_id}`,
    aUrl: a.audio_url,
    bUrl: b.audio_url,
    out: t.out_start_s,
    inStart: t.in_start_s,
    rate: 1 + (t.pitch_pct || 0) / 100,
    overlapS: (t.overlap_bars ?? 0) * bar,
    preRollS: 8 * bar,
    tailS: 8 * bar,
  };
}

const clock = (key: string): MixClock => ({
  get: () => {
    const s = useAudition.getState();
    return s.key === key && s.status !== "idle" ? s.mix : null;
  },
  subscribe: (cb) => useAudition.subscribe(cb),
});

function Deck({ side, st, onClick }: { side: "A" | "B"; st: SetTrack; onClick: () => void }) {
  return (
    <Tooltip content={`${st.track.camelot} · ${fmtBpm(st.track.bpm)} BPM · click to open the track`} side="bottom" className="min-w-0 flex-1">
      <button type="button" onClick={onClick} className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-surface">
        <span className={cn("grid size-6 shrink-0 place-items-center rounded-xs text-xs font-bold", side === "A" ? "bg-raised text-fg" : "bg-accent/20 text-accent-hover")}>{side}</span>
        <CoverArt src={st.track.cover_url} size={32} camelot={st.track.camelot} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">{st.track.name}</div>
          <div className="truncate text-xs text-fg-subtle">{st.track.artists.join(", ")}</div>
        </div>
      </button>
    </Tooltip>
  );
}

export function MixDetail({ folder, t, a, b, index, count, onPrev, onNext, onSelectTrack }: MixDetailProps) {
  const va = useTrackAnalysis(folder, a.track.id, a.analysis_state);
  const vb = useTrackAnalysis(folder, b.track.id, b.analysis_state);
  const anA = va.status === "ready" ? va.analysis : null;
  const anB = vb.status === "ready" ? vb.analysis : null;
  const key = `${t.from_id}>${t.to_id}`;
  const plan = auditionPlan(t, a, b, anA?.bar_s);
  const active = useAudition((s) => s.key === key && s.status !== "idle");
  const loading = useAudition((s) => s.key === key && s.status === "loading");
  const g = useMemo(() => (t.ready ? mixGeometry(t, anA, anB, a.track.bpm) : null), [t, anA, anB, a.track.bpm]);
  const mixClock = useMemo(() => clock(key), [key]);
  const mp = useMixPlan(folder, t.from_id, t.to_id, t.ready && !t.pending);
  const hasPlan = !!mp.plan;
  const [details, setDetails] = useSessionState("seedbox.mix.details", false);
  // "Audition" from a transition card on an engine without mix plans → the rough two-deck preview
  const legacyPlay = useCallback(() => plan && audition.toggle(plan), [plan]);
  usePlayRequests(key, mp.unavailable && !!plan, legacyPlay);

  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      <div className="flex items-center gap-2">
        <span className="label-caps">
          Transition <span className="num">{index + 1}</span> of <span className="num">{count}</span>
        </span>
        <span className="flex-1" />
        <IconButton icon={ChevronLeft} label="Previous transition" size="sm" onClick={onPrev} disabled={!onPrev} />
        <IconButton icon={ChevronRight} label="Next transition" size="sm" onClick={onNext} disabled={!onNext} />
      </div>
      <div className="flex items-center gap-2">
        <Deck side="A" st={a} onClick={() => onSelectTrack(a.track.id)} />
        <ArrowRight size={16} className="shrink-0 text-fg-subtle" />
        <Deck side="B" st={b} onClick={() => onSelectTrack(b.track.id)} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {!t.pending && <ScoreChip value={t.score} move={t.key_move} passed={t.ready} size="md" />}
        <MoveChip move={t.key_move} />
        <PitchChip pct={t.pitch_pct} />
        {t.ready && t.technique && <span className="text-sm font-semibold">{techniqueLong(t)}</span>}
        <span className="flex-1" />
        {t.warnings.length > 0 && (
          <Tooltip content={t.warnings.join(" · ")}>
            <button type="button" aria-label={`${t.warnings.length} warning${t.warnings.length === 1 ? "" : "s"}: ${t.warnings.join(" · ")}`} className="inline-flex items-center gap-1 rounded-xs px-0.5 text-xs font-semibold text-warn num">
              <AlertTriangle size={13} />
              {t.warnings.length}
            </button>
          </Tooltip>
        )}
        {!hasPlan && mp.loading && t.ready && (
          <span className="text-2xs text-fg-subtle">Loading the mix plan…</span>
        )}
        {plan && !hasPlan && !mp.loading && (
          <>
            <span className="text-2xs text-fg-subtle">Rough preview — two browser decks, not beat-locked</span>
            <Button variant="primary" icon={active ? Square : Headphones} loading={loading} kbd="Space" onClick={() => audition.toggle(plan)}>
              {active ? "Stop audition" : "Audition transition"}
            </Button>
          </>
        )}
        <Button variant="ghost" size="sm" icon={Info} aria-pressed={details} className={cn(details && "bg-raised text-fg")} onClick={() => setDetails((v) => !v)}>
          Details
        </Button>
      </div>

      {hasPlan && mp.plan ? (
        <MixEditor key={key} folder={folder} a={a} b={b} anA={anA} anB={anB} mp={{ ...mp, plan: mp.plan }} details={details} />
      ) : null}

      {!hasPlan && mp.error && !mp.unavailable && t.ready && (
        <p className="text-xs text-fg-subtle select-text">Mix editor unavailable for this pair: {mp.error.message}</p>
      )}

      {details && !hasPlan && t.ready && t.out_start_s != null && t.in_start_s != null ? (
        <MixStats t={t} out={t.out_start_s} cue={t.in_start_s} anA={anA} anB={anB} g={g} bpmB={b.track.bpm} />
      ) : null}

      {hasPlan ? null : t.ready && anA && anB ? (
        <MixWaveform
          key={key}
          a={anA}
          b={anB}
          aId={a.track.id}
          bId={b.track.id}
          aBpm={a.track.bpm}
          transition={t}
          clock={mixClock}
          onSeek={plan ? (m) => void audition.start(plan, m) : undefined}
          laneHeight={96}
          hotkeys
          toolbar={
            <>
              <span className="label-caps" title="B aligned under A at the mix point, scaled by its pitch · click to audition from there">Mix view</span>
            </>
          }
          labelA={<DeckLabel side="A" st={a} />}
          labelB={<DeckLabel side="B" st={b} />}
        />
      ) : (
        <div className="flex flex-col gap-1.5">
          <div className="flex h-6 items-center">
            <span className="label-caps">Mix view</span>
          </div>
          <WavePlaceholder view={mixPlaceholderView(t.ready, va, vb)} height={240} />
        </div>
      )}

      {details && <MixNotes t={t} hasPlan={hasPlan} g={g} />}
    </div>
  );
}

function DeckLabel({ side, st }: { side: "A" | "B"; st: SetTrack }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-xs bg-black/60 px-1.5 py-0.5 text-2xs font-semibold text-fg backdrop-blur-sm">
      <span className={side === "A" ? "text-fg-muted" : "text-accent-hover"}>{side}</span>
      <span className="max-w-[220px] truncate font-medium">{st.track.name}</span>
    </span>
  );
}
