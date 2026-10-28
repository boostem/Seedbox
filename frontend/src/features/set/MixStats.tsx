/** The mix view's cue sheet when there is no mix plan: where to bring B in, cue it, overlap, take A out and pitch B. */
import type { TrackAnalysis } from "@/api/types";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtPitch, pitchTone } from "@/lib/format";
import { fmtPrecise, type MixGeometry } from "@/features/waveform";
import { Stat } from "./bits";
import type { SetTransition } from "./model";

/** 1-based bar of time s on a track's grid, or null without an analysis. */
const barOf = (an: TrackAnalysis | null, s: number | null) => (an && s != null && an.bar_s > 0 ? Math.round((s - an.first_downbeat_s) / an.bar_s) + 1 : null);

export function MixStats({ t, out, cue, anA, anB, g, bpmB }: { t: SetTransition; out: number; cue: number; anA: TrackAnalysis | null; anB: TrackAnalysis | null; g: MixGeometry | null; bpmB: number }) {
  const pitched = bpmB * (1 + t.pitch_pct / 100);
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-md border border-line bg-surface px-4 py-3 lg:grid-cols-5">
      <Stat label="Bring B in at" sub={barOf(anA, out) ? `A bar ${barOf(anA, out)} · on the phrase` : "A's timeline"}>
        <span className="num">{fmtPrecise(out)}</span>
      </Stat>
      <Stat label="Cue B at" sub={barOf(anB, cue) ? `B bar ${barOf(anB, cue)}` : "B's timeline"}>
        <span className="num">{fmtPrecise(cue)}</span>
      </Stat>
      <Stat label="Overlap" sub={g && g.overlapS > 0 ? `${g.overlapS.toFixed(1)} s at A's tempo` : "cut on the 1"}>
        <span className="num">{t.overlap_bars ? `${t.overlap_bars} bars` : "Cut"}</span>
      </Stat>
      <Stat
        label="A out by"
        sub={t.technique === "echo out" ? (g?.swapBar != null ? `bass swapped at bar ${g.swapBar + 1} · echo A out` : "echo A out here") : g?.swapBar != null ? `swap bass at bar ${g.swapBar + 1}` : t.technique ?? undefined}
      >
        <span className="num">{g ? fmtPrecise(g.outEnd) : "—"}</span>
      </Stat>
      <Stat label="Pitch B" sub={`${fmtBpm(bpmB)} → ${fmtBpm(Math.round(pitched * 10) / 10)} BPM`}>
        <span className={cn("num", pitchTone(t.pitch_pct))}>{fmtPitch(t.pitch_pct)}</span>
      </Stat>
    </div>
  );
}
