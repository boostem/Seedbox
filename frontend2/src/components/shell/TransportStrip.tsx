import { Pause, Play, Square, Volume1, Volume2, VolumeX } from "lucide-react";
import { player, usePlayer } from "@/audio/player";
import { IconButton, Slider } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtTime } from "@/lib/format";
import { comboLabel } from "@/lib/hotkeys";

/** The persistent transport: play/pause and stop, what is playing with a seek bar, and the volume. */
export function TransportStrip() {
  const { currentId, title, subtitle, status, position, duration, volume } = usePlayer();
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
        <span className="w-10 shrink-0 text-right text-xs text-fg-subtle num">{fmtTime(loaded ? position : null)}</span>
        <Slider label="Position" className="min-w-24 flex-1" value={position} max={duration || 1} step={0.1} disabled={!loaded || !duration} onChange={(v) => player.seek(v)} />
        <span className="w-10 shrink-0 text-xs text-fg-subtle num">{fmtTime(loaded && duration ? duration : null)}</span>
      </div>
      <div className="flex items-center gap-2">
        <VolumeIcon size={16} className="text-fg-subtle" aria-hidden />
        <Slider label="Volume" className="w-24" value={volume} onChange={(v) => player.setVolume(v)} />
      </div>
    </footer>
  );
}
