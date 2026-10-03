/** A crate-to-crate link: one crate's last track → the next crate's first, scored like any transition. */
import { AlertTriangle, ArrowDown, ArrowRight, Sparkles } from "lucide-react";
import type { CrateLink } from "@/api/types";
import { Button, CoverArt, KeyBadge, MoveChip, ScoreChip, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm } from "@/lib/format";
import { PitchChip } from "@/features/set/bits";
import { techniqueShort } from "@/features/set/model";

export function LinkSummary({ link, compact }: { link: CrateLink; compact?: boolean }) {
  const t = link.transition;
  return (
    <span className={cn("flex min-w-0 flex-wrap items-center gap-1.5", compact && "flex-nowrap")}>
      <ScoreChip value={t.score} move={t.key_move} passed={t.ready} />
      <MoveChip move={t.key_move} />
      <PitchChip pct={t.pitch_pct} />
      {t.ready && t.technique && <span className="truncate text-xs font-medium text-fg-muted">{techniqueShort(t)}</span>}
      {!t.ready && <span className="text-2xs text-fg-subtle">key/BPM only until both are analysed</span>}
      {t.warnings.length > 0 && (
        <Tooltip content={t.warnings.join(" · ")}>
          <span className="inline-flex items-center gap-0.5 text-2xs font-semibold text-warn num">
            <AlertTriangle size={11} />
            {t.warnings.length}
          </span>
        </Tooltip>
      )}
    </span>
  );
}

function TrackChip({ name, artists, camelot, bpm, cover }: { name: string; artists: string[]; camelot: string; bpm: number; cover: string | null }) {
  return (
    <span className="flex min-w-0 flex-1 items-center gap-2">
      <CoverArt src={cover} size={26} camelot={camelot} />
      <span className="min-w-0">
        <span className="block truncate text-xs font-medium text-fg">{name}</span>
        <span className="block truncate text-2xs text-fg-subtle">{artists.join(", ")}</span>
      </span>
      <KeyBadge camelot={camelot} size="xs" />
      <span className="text-2xs text-fg-subtle num">{fmtBpm(bpm)}</span>
    </span>
  );
}

export interface LinkCardProps {
  link: CrateLink;
  selected?: boolean;
  onSelect?: () => void;
  onUseBetterEntry?: () => void;
  busy?: boolean;
}

/** Between two crate blocks of a setlist. */
export function LinkCard({ link, selected, onSelect, onUseBetterEntry, busy }: LinkCardProps) {
  const a = link.from_track;
  const b = link.to_track;
  const be = link.better_entry;
  return (
    <div className="relative my-1.5 pl-6">
      <span className="absolute top-0 bottom-0 left-[11px] w-px bg-gradient-to-b from-line via-accent/50 to-line" />
      <span className="absolute top-1/2 left-[5px] grid size-[13px] -translate-y-1/2 place-items-center rounded-full border border-accent/60 bg-bg">
        <ArrowDown size={9} className="text-accent-hover" />
      </span>
      <div
        role="button"
        tabIndex={0}
        onClick={onSelect}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect?.())}
        className={cn(
          "flex cursor-pointer flex-col gap-1.5 rounded-md border px-2.5 py-2 transition-colors",
          selected ? "border-accent/70 bg-accent/10" : "border-line bg-surface hover:border-line-strong hover:bg-raised/50",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className="label-caps !text-accent-hover">Crate link</span>
          <span className="flex-1" />
          <LinkSummary link={link} compact />
        </div>
        <div className="flex items-center gap-2">
          <TrackChip name={a.name} artists={a.artists} camelot={a.camelot} bpm={a.bpm} cover={a.cover_url} />
          <ArrowRight size={13} className="shrink-0 text-fg-faint" />
          <TrackChip name={b.name} artists={b.artists} camelot={b.camelot} bpm={b.bpm} cover={b.cover_url} />
        </div>
        {be && (
          <div className="flex items-center gap-2 rounded-sm bg-ok/8 px-2 py-1 text-2xs text-ok">
            <Sparkles size={11} className="shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              Opening the next crate with <span className="font-semibold">{be.track.name}</span> scores <span className="num font-semibold">{be.transition.score}</span>{" "}
              (vs <span className="num">{link.transition.score}</span>)
            </span>
            {onUseBetterEntry && (
              <Button
                size="xs"
                variant="ghost"
                loading={busy}
                onClick={(e) => {
                  e.stopPropagation();
                  onUseBetterEntry();
                }}
              >
                Move it first
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
