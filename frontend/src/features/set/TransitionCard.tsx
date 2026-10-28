import { AlertTriangle, ChevronDown, Headphones, Loader2 } from "lucide-react";
import { memo } from "react";
import type { SetTrack } from "@/api/types";
import { Button, MoveChip, ScoreChip, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { moveColor } from "@/lib/moves";
import { useApp } from "@/store/app";
import { PitchChip } from "./bits";
import { headline, type SetTransition, techniqueShort } from "./model";

export interface TransitionCardProps {
  t: SetTransition;
  a: SetTrack;
  b: SetTrack;
  folder: string;
  selected?: boolean;
  expanded?: boolean;
  dimmed?: boolean;
  worst?: boolean;
  auditioning?: boolean;
  onSelect?: () => void;
  onToggle?: () => void;
  onAudition?: () => void;
}

/** A → B suggestion between two set rows: score, key move, pitch, technique/overlap, headline; expands to notes. */
export const TransitionCard = memo(function TransitionCard({ t, a, b, folder, selected, expanded, dimmed, worst, auditioning, onSelect, onToggle, onAudition }: TransitionCardProps) {
  const liveA = useApp((s) => s.analysis[folder]?.[a.track.id]);
  const liveB = useApp((s) => s.analysis[folder]?.[b.track.id]);
  const sa = liveA ?? a.analysis_state;
  const sb = liveB ?? b.analysis_state;
  const failed = sa === "error" || sb === "error";
  const color = moveColor(t.key_move);
  const head = t.ready ? headline(t) : null;
  const warn = t.warnings.length;
  const technique = t.ready ? techniqueShort(t) : null;

  return (
    <div data-row-id={`x:${t.from_id}>${t.to_id}`} className={cn("relative pl-[30px] transition-opacity", dimmed && "pointer-events-none opacity-25")}>
      {/* connector between the two rows, in the move colour */}
      <span className="absolute top-0 bottom-0 left-[21px] w-[2px] rounded-full" style={{ background: `color-mix(in srgb, ${color} 55%, transparent)` }} />
      <div
        role="button"
        tabIndex={dimmed ? -1 : 0}
        aria-pressed={!!selected}
        aria-label={`Transition ${a.track.display_name} → ${b.track.display_name}${t.pending ? "" : `, score ${Math.round(t.score)}`}${warn ? `, ${warn} warning${warn === 1 ? "" : "s"}` : ""} — open the mix view`}
        onClick={onSelect}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;
          // the card's own Enter/Space, not the page's Space = play (or a set-row drag)
          e.preventDefault();
          e.stopPropagation();
          onSelect?.();
        }}
        className={cn(
          "my-0.5 rounded-sm border px-2 py-1 transition-colors",
          selected ? "border-accent/60 bg-accent/10" : worst ? "border-danger/30 bg-danger/[0.04] hover:bg-hover" : "border-transparent hover:border-line hover:bg-hover",
        )}
      >
        <div className="flex h-5 items-center gap-1.5">
          {t.pending ? (
            <span className="inline-flex h-[18px] w-[30px] items-center justify-center rounded-xs bg-raised text-fg-subtle">
              <Loader2 size={11} className="animate-spin" />
            </span>
          ) : (
            <Tooltip content={t.ready ? "Transition score (key + tempo + structure)" : "Key + tempo score — structure not analysed yet"}>
              <ScoreChip value={t.score} move={t.key_move} passed={t.ready} />
            </Tooltip>
          )}
          <MoveChip move={t.key_move} />
          {technique && <span className="shrink-0 text-xs font-medium whitespace-nowrap text-fg">{technique}</span>}
          <span className="min-w-0 flex-1 truncate text-xs text-fg-subtle">
            {t.pending ? (
              "scoring…"
            ) : t.ready ? null : failed ? (
              <span className="text-danger/80">no mix points — analysis failed</span>
            ) : (
              <span className="inline-flex items-center gap-1 animate-pulse-soft">analysing… mix points soon</span>
            )}
          </span>
          {warn > 0 && (
            <Tooltip content={t.warnings.join(" · ")}>
              <span className="inline-flex shrink-0 items-center gap-0.5 text-2xs font-semibold text-warn num">
                <AlertTriangle size={11} />
                {warn}
              </span>
            </Tooltip>
          )}
          <button
            type="button"
            aria-label={expanded ? "Hide notes" : "Show notes"}
            aria-expanded={expanded}
            onClick={(e) => {
              e.stopPropagation();
              onToggle?.();
            }}
            className="grid size-5 shrink-0 place-items-center rounded-xs text-fg-subtle hover:bg-raised hover:text-fg"
          >
            <ChevronDown size={13} className={cn("transition-transform", expanded && "rotate-180")} />
          </button>
        </div>
        {expanded && (
          <div className="mt-1.5 mb-0.5 space-y-1 border-t border-line/70 pt-1.5 text-xs select-text" onClick={(e) => e.stopPropagation()}>
            <div className="flex flex-wrap items-center gap-1.5 pb-0.5">
              <PitchChip pct={t.pitch_pct} />
              {worst && <span className="text-2xs font-semibold tracking-wide text-danger uppercase">weakest transition</span>}
              {head && <span className="text-fg-subtle">{head}</span>}
            </div>
            {t.notes.map((n, i) => (
              <p key={i} className={i === 0 && t.ready ? "font-medium text-fg" : "text-fg-muted"}>
                {n}
              </p>
            ))}
            {t.warnings.map((w, i) => (
              <p key={`w${i}`} className="flex gap-1.5 text-warn">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                {w}
              </p>
            ))}
            {t.ready && (
              <div className="flex gap-1.5 pt-1">
                <Button size="xs" variant="secondary" icon={Headphones} onClick={onAudition}>
                  {auditioning ? "Stop" : "Play transition"}
                </Button>
                {!selected && (
                  <Button size="xs" variant="ghost" onClick={onSelect}>
                    Open mix view
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
});
