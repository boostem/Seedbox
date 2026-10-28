/** The track view's info panels: analysis facts (tempo, grid, intro / outro, drops, vocals) and the cue list. */
import { AlertTriangle } from "lucide-react";
import type { ReactNode } from "react";
import type { SetTrack, TrackAnalysis } from "@/api/types";
import { StarRating } from "@/components/ui";
import { fmtBpm, fmtTime } from "@/lib/format";
import { cueColor, edgeColor, edgeWords, fmtPrecise, mergeCues } from "@/features/waveform";

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-h-7 items-center gap-3 border-b border-line/60 py-1 last:border-b-0">
      <span className="w-[84px] shrink-0 text-xs text-fg-subtle">{label}</span>
      <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm text-fg">{children}</span>
    </div>
  );
}

function Summary({ an, st, onSeek }: { an: TrackAnalysis; st: SetTrack; onSeek: (t: number) => void }) {
  const drops = an.sections.filter((s) => s.kind === "drop");
  const vocalBars = an.vocal_per_bar.filter((v) => v >= 0.5).length;
  const phrases = Math.max(0, Math.floor((an.bar_count - an.phrase_offset) / 8));
  const disagrees = st.track.bpm > 0 && ![0.5, 1, 2].some((r) => Math.abs(an.bpm / st.track.bpm - r) / r <= 0.03);
  const edge = (e: TrackAnalysis["intro"], which: "intro" | "outro") =>
    e.bars > 0 && e.end_s > e.start_s ? (
      <>
        <span className="num">{e.bars} bars</span>
        <span className="inline-flex items-center gap-1 text-xs" style={{ color: edgeColor(e) }}>
          <span className="size-1.5 rounded-full" style={{ background: edgeColor(e) }} />
          {edgeWords(e)}
        </span>
        <span className="text-xs text-fg-subtle num">
          {fmtTime(e.start_s)}–{fmtTime(e.end_s)}
        </span>
      </>
    ) : (
      <span className="text-fg-subtle">{which === "intro" ? "none — starts straight into the music" : "none — ends cold"}</span>
    );
  return (
    <div>
      <Fact label="Tempo">
        <span className="num">{an.bpm.toFixed(1)} BPM</span>
        <span className="text-xs text-fg-subtle">{an.bpm_source === "tunebat" ? "grid locked to tunebat" : "detected from audio"}</span>
        {disagrees && (
          <span className="inline-flex items-center gap-1 text-xs text-warn">
            <AlertTriangle size={11} /> tunebat says {fmtBpm(st.track.bpm)}
          </span>
        )}
      </Fact>
      <Fact label="Grid">
        <span className="num">{an.bar_count} bars</span>
        <span className="text-xs text-fg-subtle num">
          {phrases} phrases · first downbeat {fmtPrecise(an.first_downbeat_s)}
          {an.phrase_offset ? ` · ${an.phrase_offset}-bar pickup` : ""}
        </span>
      </Fact>
      <Fact label="Intro">{edge(an.intro, "intro")}</Fact>
      <Fact label="Outro">{edge(an.outro, "outro")}</Fact>
      <Fact label="Drops">
        {drops.length ? (
          drops.map((d, i) => (
            <button key={i} type="button" onClick={() => onSeek(d.start_s)} className="rounded-xs bg-danger/10 px-1.5 text-xs font-medium text-danger hover:bg-danger/20 num">
              {fmtTime(d.start_s)} · bar {d.start_bar + 1}
            </button>
          ))
        ) : (
          <span className="text-fg-subtle">none detected</span>
        )}
      </Fact>
      <Fact label="Vocals">
        <span className="num">{an.bar_count ? Math.round((vocalBars / an.bar_count) * 100) : 0}% of bars</span>
        <span className="text-xs text-fg-subtle">heuristic</span>
      </Fact>
    </div>
  );
}

function Cues({ an, onSeek }: { an: TrackAnalysis; onSeek: (t: number) => void }) {
  return (
    <div className="flex flex-col">
      {mergeCues(an.cues).map((c, i) => (
        <button key={i} type="button" onClick={() => onSeek(c.time_s)} className="flex h-7 items-center gap-2 rounded-xs px-1.5 text-left hover:bg-raised">
          <span className="size-2 shrink-0 rotate-45 rounded-[1px]" style={{ background: cueColor(c.label.split(" · ").pop() ?? c.label) }} />
          <span className="min-w-0 flex-1 truncate text-sm text-fg">{c.label}</span>
          <span className="text-xs text-fg-subtle num">bar {c.bar + 1}</span>
          <span className="w-10 text-right text-xs text-fg-muted num">{fmtTime(c.time_s)}</span>
        </button>
      ))}
    </div>
  );
}

export function TrackInfo({ an, st, onSeek }: { an: TrackAnalysis; st: SetTrack; onSeek: (t: number) => void }) {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
      <section className="rounded-md border border-line bg-surface px-3 py-1.5">
        <h3 className="label-caps py-1 !text-fg-muted">Analysis</h3>
        <Fact label="Track">
          <span className="num">{fmtTime(st.track.duration_s)}</span>
          {st.genre && <span className="text-xs text-fg-subtle">{st.genre}</span>}
          <StarRating rating={st.energy_rating} />
        </Fact>
        <Summary an={an} st={st} onSeek={onSeek} />
      </section>
      <section className="rounded-md border border-line bg-surface px-1.5 py-1.5">
        <h3 className="label-caps px-1.5 py-1 !text-fg-muted">Cues</h3>
        <Cues an={an} onSeek={onSeek} />
      </section>
    </div>
  );
}
