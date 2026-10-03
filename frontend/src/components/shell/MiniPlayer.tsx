import { Play, Square, Volume1, Volume2, VolumeX } from "lucide-react";
import { useRef } from "react";
import { player, usePlayer } from "@/audio/player";
import { CoverArt, IconButton, Kbd, KeyBadge, PeaksWaveform, ProgressBar, Slider, Spinner, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtTime } from "@/lib/format";
import { useApp } from "@/store/app";

/** Bottom bar: what's playing (preview clip or file), transport, clip waveform, volume. */
export function MiniPlayer() {
  const { currentId, track, status, position, duration, volume, source, error } = usePlayer();
  const peaks = useApp((s) => (currentId && source === "preview" ? s.previews[currentId]?.peaks : null));
  const selection = useApp((s) => s.selection);
  const lastVol = useRef(0.8);
  const idle = !currentId;
  const playing = status === "playing";
  const loading = status === "loading";
  const progress = duration ? position / duration : 0;
  const VolIcon = volume === 0 ? VolumeX : volume < 0.5 ? Volume1 : Volume2;

  return (
    <footer className="flex h-14 shrink-0 items-center gap-4 border-t border-line bg-panel px-3">
      {/* now playing */}
      <div className="flex w-[300px] min-w-0 items-center gap-2.5">
        {idle ? (
          <>
            <div className="grid size-10 place-items-center rounded-sm border border-dashed border-line-strong text-fg-faint">
              <Play size={14} />
            </div>
            <div className="min-w-0 text-xs text-fg-subtle">
              {selection ? (
                <>
                  <div className="truncate text-fg-muted">{selection.track.display_name}</div>
                  <div>
                    <Kbd combo="Space" subtle /> to play {selection.kind === "preview" ? "preview" : "track"}
                  </div>
                </>
              ) : (
                <>
                  <div className="text-fg-muted">Nothing playing</div>
                  <div>Select a track, then <Kbd combo="Space" subtle /></div>
                </>
              )}
            </div>
          </>
        ) : (
          <>
            <CoverArt src={track?.cover_url} size={40} camelot={track?.camelot} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{track?.name ?? currentId}</div>
              <div className="flex items-center gap-1.5 text-xs text-fg-muted">
                <span className="truncate">{track?.artists.join(", ")}</span>
              </div>
            </div>
            {track && (
              <div className="flex shrink-0 flex-col items-end gap-0.5">
                <KeyBadge camelot={track.camelot} keyName={track.key} size="xs" />
                <span className="text-2xs text-fg-subtle num">{fmtBpm(track.bpm)} BPM</span>
              </div>
            )}
          </>
        )}
      </div>

      {/* transport + waveform */}
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <Tooltip content={playing || loading ? "Stop (Space)" : "Play (Space)"}>
          <button
            type="button"
            aria-label={playing || loading ? "Stop" : "Play"}
            disabled={idle && !selection}
            onClick={() => (idle ? player.toggleSelection() : playing || loading ? player.stop() : player.resume())}
            className={cn(
              "grid size-8 shrink-0 place-items-center rounded-full transition-colors disabled:opacity-30",
              playing ? "bg-accent text-white hover:bg-accent-hover" : "bg-raised text-fg hover:bg-overlay",
            )}
          >
            {loading ? <Spinner size={14} /> : playing ? <Square size={12} fill="currentColor" /> : <Play size={14} fill="currentColor" className="ml-0.5" />}
          </button>
        </Tooltip>
        <span className="w-9 shrink-0 text-right text-xs text-fg-muted num">{idle ? "" : fmtTime(position)}</span>
        <div className="min-w-0 flex-1">
          {idle ? (
            <div className="h-px w-full bg-line" />
          ) : peaks?.length ? (
            <PeaksWaveform peaks={peaks} progress={progress} height={30} onSeek={(f) => player.seekFraction(f)} playedColor="var(--color-accent-hover)" />
          ) : (
            <div
              className="cursor-pointer py-2"
              onPointerDown={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                player.seekFraction((e.clientX - r.left) / r.width);
              }}
            >
              <ProgressBar value={loading && !duration ? null : progress} size="sm" />
            </div>
          )}
        </div>
        <span className="w-9 shrink-0 text-xs text-fg-subtle num">{idle ? "" : fmtTime(duration || track?.duration_s)}</span>
      </div>

      {/* source + volume */}
      <div className="flex w-[220px] shrink-0 items-center justify-end gap-2">
        {!idle && (
          <span className={cn("text-2xs font-medium tracking-wide uppercase", error ? "text-danger" : "text-fg-subtle")}>
            {error ? "Error" : source === "preview" ? "Preview clip" : "Full track"}
          </span>
        )}
        <IconButton
          icon={VolIcon}
          label={volume === 0 ? "Unmute" : "Mute"}
          size="sm"
          onClick={() => {
            if (volume > 0) {
              lastVol.current = volume;
              player.setVolume(0);
            } else player.setVolume(lastVol.current || 0.8);
          }}
        />
        <Slider value={volume} onChange={player.setVolume} className="w-20" label="Volume" color="var(--color-fg-muted)" />
      </div>
    </footer>
  );
}
