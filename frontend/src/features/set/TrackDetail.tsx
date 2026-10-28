import { FolderOpen, Info, Play, Square } from "lucide-react";
import { api } from "@/api/client";
import type { SetTrack } from "@/api/types";
import { player, useIsPlaying } from "@/audio/player";
import { Button, CoverArt, IconButton, KeyBadge } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm } from "@/lib/format";
import { TrackWaveform } from "@/features/waveform";
import { analysisStateText } from "./bits";
import { TempoWarning } from "./TempoWarning";
import { TrackInfo } from "./TrackInfo";
import { TrackPartners, type TrackPartnersProps } from "./TrackPartners";
import { useTrackAnalysis } from "./useAnalysis";
import { useSessionState } from "./useSessionState";
import { WavePlaceholder } from "./WavePlaceholder";

export interface TrackDetailProps extends TrackPartnersProps {
  index: number;
  total: number;
  isSeed: boolean;
}

function play(st: SetTrack, at?: number) {
  const cur = player.isCurrent(st.track.id);
  if (at != null && cur) player.seek(at);
  else if (at != null) void player.play(st.track.id, st.audio_url, { track: st.track, source: "file", startAt: at });
  else player.toggleFile(st.track.id, st.audio_url, { track: st.track });
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
          <Button variant="secondary" icon={playing ? Square : Play} onClick={() => play(st)} kbd="Space">
            {playing ? "Stop" : "Play"}
          </Button>
          <Button variant="ghost" icon={Info} aria-pressed={info} className={cn(info && "bg-raised text-fg")} onClick={() => setInfo((v) => !v)}>
            Info
          </Button>
          <IconButton icon={FolderOpen} label="Reveal in Finder" onClick={() => void api.reveal(st.path)} />
        </div>
      </div>

      {view.status === "ready" && <TempoWarning st={st} an={view.analysis} />}

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

      {info && view.status === "ready" && <TrackInfo an={view.analysis} st={st} onSeek={seek} />}

      <TrackPartners {...props} />
    </div>
  );
}
