import { AlertTriangle, ArrowDownToLine, ArrowUpToLine, FolderOpen, Info, Play, Square } from "lucide-react";
import { type ReactNode, useState } from "react";
import { api } from "@/api/client";
import type { Partner, SetTrack, TrackAnalysis } from "@/api/types";
import { player, useIsPlaying } from "@/audio/player";
import { Button, CoverArt, EmptyState, IconButton, KeyBadge, MoveChip, ScoreChip, Spinner, StarRating, Tabs } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtTime } from "@/lib/format";
import { useAsync } from "@/lib/useAsync";
import { useSetRefreshKey } from "@/store/app";
import { cueColor, edgeColor, edgeWords, fmtPrecise, mergeCues, TrackWaveform } from "@/features/waveform";
import { analysisStateText } from "./bits";
import { techniqueLong, techniqueShort, tempoMismatch } from "./model";
import { type AnalysisView, useTrackAnalysis } from "./useAnalysis";
import { useSessionState } from "./useSessionState";

export interface TrackDetailProps {
  folder: string;
  st: SetTrack;
  index: number;
  total: number;
  isSeed: boolean;
  /** Ids currently right after / before this track (to flag partners already in place). */
  nextId: string | null;
  prevId: string | null;
  onPlace: (partnerId: string, where: "after" | "before") => void;
  onSelectTrack: (id: string) => void;
  onPartners?: (ps: Partner[]) => void;
}

function play(st: SetTrack, at?: number) {
  const cur = player.isCurrent(st.track.id);
  if (at != null && cur) player.seek(at);
  else if (at != null) void player.play(st.track.id, st.audio_url, { track: st.track, source: "file", startAt: at });
  else player.toggleFile(st.track.id, st.audio_url, { track: st.track });
}

export function WavePlaceholder({ view, height = 190 }: { view: AnalysisView; height?: number }) {
  const msg =
    view.status === "pending"
      ? view.state === "running"
        ? "Analysing audio — waveform, cues and mix points appear in a moment…"
        : "Queued for analysis — waveform, cues and mix points appear when it's done."
      : view.status === "error"
        ? view.message
        : "Loading analysis…";
  return (
    <div style={{ height }} className={cn("grid place-items-center rounded-sm border border-dashed", view.status === "error" ? "border-danger/40 bg-danger/[0.04]" : "border-line-strong bg-bg/60")}>
      <div className={cn("flex items-center gap-2 text-sm", view.status === "error" ? "text-danger" : "text-fg-subtle")}>
        {view.status === "error" ? <AlertTriangle size={15} /> : <Spinner size={14} className={view.status === "pending" && view.state === "queued" ? "text-fg-faint" : "text-accent-hover"} />}
        <span className="select-text">{msg}</span>
      </div>
    </div>
  );
}

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

function Partners({ folder, st, nextId, prevId, onPlace, onSelectTrack, onPartners }: Pick<TrackDetailProps, "folder" | "st" | "nextId" | "prevId" | "onPlace" | "onSelectTrack" | "onPartners">) {
  const [dir, setDir] = useState<"out" | "in">("out");
  const refresh = useSetRefreshKey(folder, 800);
  const q = useAsync(
    async (signal) => {
      const ps = await api.partners(folder, st.track.id, dir, { signal, quiet: true });
      onPartners?.(ps);
      return ps;
    },
    [folder, st.track.id, dir, refresh],
  );
  const ps = q.data;
  const adjacent = dir === "out" ? nextId : prevId;
  return (
    <section className="rounded-md border border-line bg-surface">
      <header className="flex items-center gap-3 border-b border-line px-3 py-2">
        <h3 className="label-caps !text-fg-muted">Mix partners</h3>
        <Tabs
          variant="segmented"
          value={dir}
          onChange={setDir}
          items={[
            { id: "out", label: "Best next" },
            { id: "in", label: "Best previous" },
          ]}
        />
        <span className="flex-1" />
        {q.loading && <Spinner size={12} className="text-fg-subtle" />}
        <span className="text-2xs text-fg-subtle">{dir === "out" ? "this track's outro → their intro" : "their outro → this track's intro"}</span>
      </header>
      {q.error && !ps ? (
        <p className="px-3 py-3 text-sm text-danger select-text">{q.error.message}</p>
      ) : ps && ps.length === 0 ? (
        <EmptyState compact title="No partners" description="This crate has no other tracks." />
      ) : (
        <div className={cn("flex flex-col py-1", q.loading && ps && "opacity-70")}>
          {(ps ?? []).map((p) => {
            const t = p.transition;
            const here = p.track.id === adjacent;
            return (
              <div key={p.track.id} className="group flex h-9 items-center gap-2 px-3 hover:bg-hover">
                <ScoreChip value={t.score} move={t.key_move} passed={t.ready} />
                <CoverArt src={p.track.cover_url} size={24} camelot={p.track.camelot} />
                <button type="button" onClick={() => onSelectTrack(p.track.id)} className="min-w-0 flex-1 truncate text-left text-sm hover:underline">
                  <span className="text-fg">{p.track.name}</span> <span className="text-fg-subtle">{p.track.artists.join(", ")}</span>
                </button>
                <KeyBadge camelot={p.track.camelot} keyName={p.track.key} size="xs" />
                <span className="w-8 text-right text-xs text-fg-muted num">{fmtBpm(p.track.bpm)}</span>
                <MoveChip move={t.key_move} size="xs" />
                <span className="hidden w-[92px] truncate text-xs text-fg-subtle xl:inline" title={(t.ready && techniqueLong(t)) || undefined}>
                  {(t.ready && techniqueShort(t)) || "key/BPM only"}
                </span>
                {here ? (
                  <span className="w-[92px] text-center text-2xs font-semibold tracking-wide text-ok uppercase">{dir === "out" ? "already next" : "already before"}</span>
                ) : (
                  <Button
                    size="xs"
                    variant="outline"
                    className="w-[92px]"
                    icon={dir === "out" ? ArrowDownToLine : ArrowUpToLine}
                    onClick={() => onPlace(p.track.id, dir === "out" ? "after" : "before")}
                  >
                    {dir === "out" ? "Insert after" : "Insert before"}
                  </Button>
                )}
              </div>
            );
          })}
          {!ps && (
            <div className="flex h-24 items-center justify-center">
              <Spinner className="text-fg-subtle" />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export function TrackDetail(props: TrackDetailProps) {
  const { folder, st, index, total, isSeed } = props;
  const t = st.track;
  const view = useTrackAnalysis(folder, t.id, st.analysis_state);
  const playing = useIsPlaying(t.id);
  const [info, setInfo] = useSessionState("seedbox.track.info", false);
  const seek = (s: number) => play(st, s);
  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      <div className="flex items-center gap-3.5">
        <CoverArt src={t.cover_url} size={56} camelot={t.camelot} rounded="md" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-2xs font-semibold tracking-wide text-fg-subtle uppercase">
            <span className="num">
              Track {index + 1} of {total}
            </span>
            {isSeed && <span className="rounded-xs bg-accent/15 px-1 text-accent-hover">Seed</span>}
          </div>
          <h2 className="truncate text-lg font-semibold">{t.name}</h2>
          <div className="flex min-w-0 items-center gap-2 text-sm text-fg-muted">
            <span className="truncate">{t.artists.join(", ")}</span>
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <div className="flex items-center gap-2">
            <KeyBadge camelot={t.camelot} keyName={t.key} size="md" />
            <span className="text-md font-semibold num">{fmtBpm(t.bpm)}</span>
            <span className="text-xs text-fg-subtle">BPM</span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button variant={playing ? "primary" : "secondary"} icon={playing ? Square : Play} onClick={() => play(st)} kbd="Space">
            {playing ? "Stop" : "Play"}
          </Button>
          <Button variant="ghost" icon={Info} aria-pressed={info} className={cn(info && "bg-raised text-fg")} onClick={() => setInfo((v) => !v)}>
            Info
          </Button>
          <IconButton icon={FolderOpen} label="Reveal in Finder" onClick={() => void api.reveal(st.path)} />
        </div>
      </div>

      {view.status === "ready" && tempoMismatch(t.bpm, view.analysis) && (
        <div className="flex items-center gap-3 rounded-md border border-danger/40 bg-danger/[0.07] px-3 py-2">
          <AlertTriangle size={16} className="shrink-0 text-danger" />
          <div className="min-w-0 flex-1 text-sm">
            <div className="font-semibold text-danger">Probably a wrong download</div>
            <div className="text-fg-muted select-text">
              Tempo doesn't match tunebat: the audio runs at {view.analysis.bpm.toFixed(1)} BPM, tunebat says {fmtBpm(t.bpm)}. Check the file; if it's the wrong song, delete it and download another version.
            </div>
          </div>
          <Button size="sm" variant="danger" icon={FolderOpen} onClick={() => void api.reveal(st.path)}>
            Reveal in Finder
          </Button>
        </div>
      )}

      {view.status === "ready" ? (
        <TrackWaveform
          key={t.id}
          analysis={view.analysis}
          trackId={t.id}
          onSeek={seek}
          height={150}
          hotkeys
          toolbar={
            <>
              <span className="label-caps" title="click to play from · drag to scroll · ⌘-scroll to zoom">Waveform</span>
              {view.stale && <span className="text-2xs text-warn">re-analysing…</span>}
            </>
          }
        />
      ) : (
        <div className="flex flex-col gap-1.5">
          <div className="flex h-6 items-center gap-2">
            <span className="label-caps">Waveform</span>
            <span className="text-2xs text-fg-subtle">{analysisStateText(st.analysis_state)}</span>
          </div>
          <WavePlaceholder view={view} height={186} />
        </div>
      )}

      {info && view.status === "ready" && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
          <section className="rounded-md border border-line bg-surface px-3 py-1.5">
            <h3 className="label-caps py-1 !text-fg-muted">Analysis</h3>
            <Fact label="Track">
              <span className="num">{fmtTime(t.duration_s)}</span>
              {st.genre && <span className="text-xs text-fg-subtle">{st.genre}</span>}
              <StarRating rating={st.energy_rating} />
            </Fact>
            <Summary an={view.analysis} st={st} onSeek={seek} />
          </section>
          <section className="rounded-md border border-line bg-surface px-1.5 py-1.5">
            <h3 className="label-caps px-1.5 py-1 !text-fg-muted">Cues</h3>
            <Cues an={view.analysis} onSeek={seek} />
          </section>
        </div>
      )}

      <Partners {...props} />
    </div>
  );
}
