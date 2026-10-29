import { Pause, Play, Square, Volume1, Volume2, VolumeX } from "lucide-react";
import { type CSSProperties, useLayoutEffect, useRef } from "react";
import { player, usePlayer } from "@/audio/player";
import { IconButton, Slider } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtTime } from "@/lib/format";
import { comboLabel } from "@/lib/hotkeys";

/** The persistent transport: play/pause and stop, what is playing with a seek bar, and the volume. */
export function TransportStrip() {
  const currentId = usePlayer((s) => s.currentId);
  const title = usePlayer((s) => s.title);
  const subtitle = usePlayer((s) => s.subtitle);
  const status = usePlayer((s) => s.status);
  const duration = usePlayer((s) => s.duration);
  const volume = usePlayer((s) => s.volume);
  const loaded = currentId !== null;
  const playing = status === "playing" || status === "loading";
  const VolumeIcon = volume === 0 ? VolumeX : volume < 0.5 ? Volume1 : Volume2;
  return (
    <footer aria-label="Transport" className="grid h-transport shrink-0 grid-cols-[auto_1fr_auto] items-center gap-4 border-t border-line bg-panel px-3">
      <div className="flex items-center gap-1">
        <IconButton
          icon={playing ? Pause : Play}
          label={playing ? "Pause" : "Play"}
          tooltip={`${playing ? "Pause" : "Play"} (Space)`}
          size="lg"
          variant="secondary"
          disabled={!loaded}
          loading={status === "loading"}
          onClick={() => player.playPause()}
        />
        <IconButton icon={Square} label="Stop" tooltip={`Stop (${comboLabel("Mod+.")})`} size="md" disabled={!loaded} onClick={() => player.stop()} />
      </div>
      <div className="flex min-w-0 items-center gap-3">
        <div className="w-64 min-w-0 shrink">
          <div className={cn("truncate text-sm font-medium", loaded ? "text-fg" : "text-fg-faint")}>{loaded ? (title ?? currentId) : "Nothing playing"}</div>
          <div className="truncate text-xs text-fg-subtle">{loaded ? (subtitle ?? "") : "Play a track from the library or a set"}</div>
        </div>
        <SeekBar loaded={loaded} duration={duration} />
        <span className="w-10 shrink-0 text-xs text-fg-subtle num">{fmtTime(loaded && duration ? duration : null)}</span>
      </div>
      <div className="flex items-center gap-2">
        <VolumeIcon size={16} className="text-fg-subtle" aria-hidden />
        <Slider label="Volume" className="w-24" value={volume} onChange={(v) => player.setVolume(v)} />
      </div>
    </footer>
  );
}

/**
 * The elapsed time and the seek bar. The player's `position` changes 25 times a second, so they are written through
 * refs from a store subscription instead of re-rendering (frontend2/PERFORMANCE.md).
 */
function SeekBar({ loaded, duration }: { loaded: boolean; duration: number }) {
  const time = useRef<HTMLSpanElement>(null);
  const range = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => {
    const draw = (position: number) => {
      const max = duration || 1;
      if (time.current) time.current.textContent = fmtTime(loaded ? position : null);
      if (range.current) {
        range.current.value = String(position);
        range.current.style.setProperty("--sb-pct", `${Math.min(100, (position / max) * 100)}%`);
      }
    };
    draw(usePlayer.getState().position);
    return usePlayer.subscribe((s, prev) => {
      if (s.position !== prev.position) draw(s.position);
    });
  }, [loaded, duration]);
  return (
    <>
      <span ref={time} className="w-10 shrink-0 text-right text-xs text-fg-subtle num" />
      <input
        ref={range}
        type="range"
        aria-label="Position"
        className="sb-range min-w-24 flex-1"
        style={{ "--sb-pct": "0%" } as CSSProperties}
        min={0}
        max={duration || 1}
        step={0.1}
        defaultValue={0}
        disabled={!loaded || !duration}
        onChange={(e) => player.seek(Number(e.target.value))}
      />
    </>
  );
}
