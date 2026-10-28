/** Dense version-picker table for tunebat search results. */
import { ArrowRight } from "lucide-react";
import { memo, type ReactNode, type Ref } from "react";
import type { Track } from "@/api/types";
import { Badge, CoverArt, IconButton, KeyBadge, Table, TableScroll, TBody, Td, Th, THead, Tooltip, Tr } from "@/components/ui";
import { PreviewButton } from "@/features/crate/PreviewControls";
import { fmtReleaseDate } from "@/features/crate/trackMeta";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtTime } from "@/lib/format";
import { diffFromLead, type ResultRow } from "./versions";

export interface ResultsTableProps {
  rows: ResultRow[];
  selectedId: string | null;
  onSelect: (t: Track) => void;
  onOpen: (t: Track) => void;
  grouped: boolean;
  footer?: ReactNode;
  scrollRef?: Ref<HTMLDivElement>;
  onScroll?: (top: number) => void;
}

export function ResultsTable({ rows, selectedId, onSelect, onOpen, grouped, footer, scrollRef, onScroll }: ResultsTableProps) {
  return (
    <TableScroll ref={scrollRef} className="flex-1" onScroll={(e) => onScroll?.(e.currentTarget.scrollTop)}>
      <Table className="table-fixed">
        <THead>
          <Tr interactive={false}>
            <Th width={50} className="pl-5" aria-label="Preview" />
            <Th width={38} aria-label="Cover" />
            <Th width="30%">Title</Th>
            <Th width="16%">Artists</Th>
            <Th>Album</Th>
            <Th width={96}>Released</Th>
            <Th width={54} align="right">
              Length
            </Th>
            <Th width={52} align="center">
              Key
            </Th>
            <Th width={50} align="right">
              BPM
            </Th>
            <Th width={84} className="pl-3">
              Popularity
            </Th>
            <Th width={44} className="pr-5" aria-label="Open" />
          </Tr>
        </THead>
        <TBody>
          {rows.map((r) => (
            <ResultRowView key={r.track.id} row={r} selected={r.track.id === selectedId} grouped={grouped} onSelect={onSelect} onOpen={onOpen} />
          ))}
        </TBody>
      </Table>
      {footer}
    </TableScroll>
  );
}

const ResultRowView = memo(function ResultRowView({
  row,
  selected,
  grouped,
  onSelect,
  onOpen,
}: {
  row: ResultRow;
  selected: boolean;
  grouped: boolean;
  onSelect: (t: Track) => void;
  onOpen: (t: Track) => void;
}) {
  const { track: t, parts, groupSize, groupIndex, lead } = row;
  const child = groupSize > 1 && groupIndex > 0;
  const d = child ? diffFromLead(t, lead) : null;
  // inside a version group, values equal to the lead version step back so the differences pop out
  const same = (differs: boolean | undefined) => (d && !differs ? "text-fg-subtle" : "");
  const strong = (differs: boolean | undefined) => (d && differs ? "font-medium text-fg" : "");

  return (
    <Tr
      data-row-id={t.id}
      selected={selected}
      rowHeight={34}
      className={cn(grouped && groupSize > 1 && groupIndex === groupSize - 1 && "[&>td]:border-b-line-strong", grouped && groupSize > 1 && groupIndex === 0 && "[&>td]:border-b-line/30", child && grouped && "[&>td]:border-b-line/30")}
      onClick={() => onSelect(t)}
      onDoubleClick={(e) => {
        if ((e.target as HTMLElement).closest("button")) return;
        onOpen(t);
      }}
    >
      <Td className="pl-5">
        <PreviewButton track={t} />
      </Td>
      <Td className="px-1">
        <CoverArt src={t.cover_url} size={26} rounded="xs" camelot={t.camelot} />
      </Td>
      <Td truncate>
        <span className="flex min-w-0 items-center gap-1.5">
          {child && grouped && <span aria-hidden className="ml-1 h-2.5 w-2 shrink-0 -translate-y-1 rounded-bl-[3px] border-b border-l border-fg-faint" />}
          <span className={cn("truncate", child && grouped ? "text-fg-muted" : "font-medium text-fg", strong(d?.title))} title={t.name}>
            {parts.title}
          </span>
          {parts.version && (
            <Badge size="xs" tone={/remix|edit|vip|bootleg|rework|flip/i.test(parts.version) ? "boost" : /extended|club|original|dub/i.test(parts.version) ? "info" : "neutral"} className="max-w-[160px] truncate">
              <span className="truncate">{parts.version}</span>
            </Badge>
          )}
          {parts.feat && <span className="truncate text-xs text-fg-subtle">{parts.feat}</span>}
          {groupSize > 1 && groupIndex === 0 && (
            <Tooltip content={grouped ? `${groupSize} versions of this song — values that differ from this one are highlighted below` : `${groupSize} versions of this song in the results`}>
              <span className="ml-auto inline-flex h-4 shrink-0 items-center rounded-xs border border-line-strong px-1 text-2xs text-fg-subtle num">{groupSize} versions</span>
            </Tooltip>
          )}
        </span>
      </Td>
      <Td truncate className={cn("text-fg-muted", same(d?.artists), strong(d?.artists))} title={t.artists.join(", ")}>
        {t.artists.join(", ")}
      </Td>
      <Td truncate className={cn("text-fg-muted", same(d?.album), strong(d?.album))} title={t.album}>
        {t.album}
      </Td>
      <Td num className={cn("text-fg-muted", same(d?.date), strong(d?.date))}>
        {fmtReleaseDate(t.release_date)}
      </Td>
      <Td num align="right" className={cn(same(d?.duration), strong(d?.duration))}>
        {fmtTime(t.duration_s)}
      </Td>
      <Td align="center">
        <KeyBadge camelot={t.camelot} keyName={t.key} size="sm" dim={!!d && !d.key} />
      </Td>
      <Td num align="right" className={cn(same(d?.bpm), strong(d?.bpm))}>
        {fmtBpm(t.bpm)}
      </Td>
      <Td className="pl-3">
        <Popularity value={t.popularity} />
      </Td>
      <Td className="pr-5" align="right">
        <IconButton
          icon={ArrowRight}
          label="Use as seed"
          tooltip="Use as seed → recommendations (Enter)"
          size="sm"
          variant={selected ? "secondary" : "ghost"}
          className={cn(!selected && "opacity-0 group-hover/row:opacity-100 group-focus-within/row:opacity-100")}
          onClick={(e) => {
            e.stopPropagation();
            onOpen(t);
          }}
        />
      </Td>
    </Tr>
  );
});

function Popularity({ value }: { value: number }) {
  const v = Math.max(0, Math.min(100, value || 0));
  return (
    <Tooltip content={`Spotify popularity ${v}/100`}>
      <span className="flex items-center gap-1.5">
        <span className="relative h-1 w-8 overflow-hidden rounded-full bg-line-strong/70">
          <span className="absolute inset-y-0 left-0 rounded-full bg-fg-subtle" style={{ width: `${v}%` }} />
        </span>
        <span className="w-5 text-right text-xs text-fg-subtle num">{v}</span>
      </span>
    </Tooltip>
  );
}
