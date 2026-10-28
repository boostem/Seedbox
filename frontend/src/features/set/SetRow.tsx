import { AlertTriangle, GripVertical, Play, Square } from "lucide-react";
import { type HTMLAttributes, memo, type Ref } from "react";
import type { SetTrack } from "@/api/types";
import { api } from "@/api/client";
import { player, useIsPlaying, usePlayer } from "@/audio/player";
import { CoverArt, KeyBadge, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm } from "@/lib/format";
import { useApp } from "@/store/app";
import { AnalysisIcon } from "./bits";
import { tempoMismatch, tempoMismatchText } from "./model";
import { useTrackAnalysis } from "./useAnalysis";

export interface SetRowProps {
  st: SetTrack;
  folder: string;
  selected?: boolean;
  isSeed?: boolean;
  onSelect?: () => void;
  /** Drag handle wiring from useSortable (keyboard activator). */
  handleRef?: Ref<HTMLButtonElement>;
  handleProps?: HTMLAttributes<HTMLButtonElement>;
  /** Rendered in the DragOverlay. */
  overlay?: boolean;
  /** The row's original slot while it's being dragged. */
  ghost?: boolean;
}

function RowProgress({ id }: { id: string }) {
  const f = usePlayer((s) => (s.currentId === id && s.duration ? s.position / s.duration : null));
  if (f == null) return null;
  return (
    <span className="pointer-events-none absolute right-0 bottom-0 left-0 h-[2px] bg-line">
      <span className="block h-full bg-accent" style={{ width: `${Math.min(100, f * 100)}%` }} />
    </span>
  );
}

/** One track of the set: handle · position · cover · title/artists · key · BPM · state · play (genre, duration, energy live in the track Info panel). */
export const SetRow = memo(function SetRow({ st, folder, selected, isSeed, onSelect, handleRef, handleProps, overlay, ghost }: SetRowProps) {
  const t = st.track;
  const playing = useIsPlaying(t.id);
  const liveState = useApp((s) => s.analysis[folder]?.[t.id]);
  const live = liveState ?? st.analysis_state;
  const an = useTrackAnalysis(folder, overlay ? null : t.id, st.analysis_state);
  const wrong = an.status === "ready" && tempoMismatch(t.bpm, an.analysis) ? an.analysis.bpm : null;
  return (
    <div
      data-row-id={`t:${t.id}`}
      onClick={onSelect}
      className={cn(
        "group relative flex h-11 items-center gap-2 rounded-sm pr-2 pl-1 transition-colors",
        overlay ? "bg-overlay shadow-pop ring-1 ring-accent/50" : selected ? "bg-selected" : "hover:bg-hover",
        ghost && "opacity-30",
      )}
    >
      {selected && !overlay && <span className="absolute top-1.5 bottom-1.5 left-0 w-[2px] rounded-full bg-accent" />}
      <Tooltip content="Drag to re-order — keyboard: focus, Space, ↑↓, Space">
        <button
          ref={handleRef}
          type="button"
          aria-label={`Move ${t.display_name}`}
          className={cn("grid h-8 w-4 shrink-0 cursor-grab place-items-center rounded-xs text-fg-faint group-hover:text-fg-subtle hover:bg-raised active:cursor-grabbing", overlay && "text-fg-muted")}
          onClick={(e) => e.stopPropagation()}
          {...handleProps}
        >
          <GripVertical size={14} />
        </button>
      </Tooltip>
      <span className={cn("w-5 shrink-0 text-right text-xs font-semibold num", selected ? "text-accent-hover" : "text-fg-subtle")}>{String(st.order).padStart(2, "0")}</span>
      <div className="relative shrink-0">
        <CoverArt src={t.cover_url} size={32} camelot={t.camelot} />
        <button
          type="button"
          aria-label={playing ? `Stop ${t.display_name}` : `Play ${t.display_name}`}
          onClick={(e) => {
            e.stopPropagation();
            onSelect?.();
            player.toggleFile(t.id, st.audio_url, { track: t });
          }}
          className={cn(
            "absolute inset-0 grid place-items-center rounded-sm bg-black/55 text-white transition-opacity",
            playing ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
          )}
        >
          {playing ? <Square size={11} fill="currentColor" /> : <Play size={13} fill="currentColor" className="ml-0.5" />}
        </button>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className={cn("truncate text-base font-medium", playing ? "text-accent-hover" : "text-fg")}>{t.name}</span>
          {isSeed && <span className="shrink-0 rounded-xs bg-accent/15 px-1 text-2xs font-semibold text-accent-hover">SEED</span>}
        </div>
        <div className="truncate text-xs text-fg-muted">
          {t.artists.join(", ")}
        </div>
      </div>
      {wrong != null && (
        <Tooltip content={`${tempoMismatchText(t.bpm, wrong)} Click to reveal the file in Finder.`}>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              void api.reveal(st.path);
            }}
            className="inline-flex h-[18px] shrink-0 items-center gap-1 rounded-xs bg-danger/15 px-1 text-2xs font-semibold text-danger ring-1 ring-danger/40 hover:bg-danger/25"
          >
            <AlertTriangle size={11} />
            WRONG FILE?
          </button>
        </Tooltip>
      )}
      <KeyBadge camelot={t.camelot} keyName={t.key} size="sm" />
      <span className="w-10 shrink-0 text-right text-sm font-medium text-fg num">{fmtBpm(t.bpm)}</span>
      <AnalysisIcon state={live} />
      <RowProgress id={t.id} />
    </div>
  );
});
