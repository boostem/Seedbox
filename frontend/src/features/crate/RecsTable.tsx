/** The crate's recommendation table: seed row first, then recs (score order by default). */
import { memo, type MouseEvent, type KeyboardEvent, type Ref } from "react";
import type { Rec, Track } from "@/api/types";
import { Badge, Checkbox, CoverArt, KeyBadge, MoveChip, ScoreChip, Table, TableScroll, TBody, Td, Th, THead, Tooltip, Tr, type SortDir } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtPitch, fmtTime } from "@/lib/format";
import { PreviewButton, PreviewWave } from "./PreviewControls";
import { pitchTone, pitchVsSeed } from "./trackMeta";

export interface CrateRow {
  track: Track;
  isSeed: boolean;
  rec: Rec | null;
  existingPath: string | null;
}

export type SortKey = "score" | "title" | "key" | "bpm" | "pitch" | "energy" | "length";

export interface RecsTableProps {
  rows: CrateRow[];
  seed: Track;
  ticks: Set<string>;
  selectedId: string | null;
  sort: { key: SortKey; dir: "asc" | "desc" };
  onSort: (k: SortKey) => void;
  onSelect: (id: string) => void;
  onTick: (id: string, value: boolean, e: MouseEvent | KeyboardEvent) => void;
  allState: "all" | "some" | "none";
  onTickAll: (value: boolean) => void;
  scrollRef?: Ref<HTMLDivElement>;
}

export function RecsTable({ rows, seed, ticks, selectedId, sort, onSort, onSelect, onTick, allState, onTickAll, scrollRef }: RecsTableProps) {
  const dir = (k: SortKey): SortDir => (sort.key === k ? sort.dir : null);
  return (
    <TableScroll ref={scrollRef} className="flex-1">
      <Table className="table-fixed">
        <THead>
          <Tr interactive={false}>
            <Th width={42} className="pl-5">
              <Checkbox
                checked={allState === "all"}
                indeterminate={allState === "some"}
                onChange={(v) => onTickAll(v)}
                title={allState === "none" ? "Tick all" : "Untick all"}
              />
            </Th>
            <Th width={32} aria-label="Preview" />
            <Th width={36} aria-label="Cover" />
            <Th sort={dir("title")} onSort={() => onSort("title")}>
              Track
            </Th>
            <Th width={52} align="center" sort={dir("score")} onSort={() => onSort("score")}>
              Score
            </Th>
            <Th width={88}>Move</Th>
            <Th width={50} align="center" sort={dir("key")} onSort={() => onSort("key")}>
              Key
            </Th>
            <Th width={52} align="right" sort={dir("bpm")} onSort={() => onSort("bpm")}>
              BPM
            </Th>
            <Th width={64} align="right" sort={dir("pitch")} onSort={() => onSort("pitch")}>
              Pitch
            </Th>
            <Th width={68} align="right" sort={dir("energy")} onSort={() => onSort("energy")}>
              Energy
            </Th>
            <Th width={66} align="right" sort={dir("length")} onSort={() => onSort("length")}>
              Length
            </Th>
            <Th width="14%" className="pl-3">
              Why
            </Th>
            <Th width={146} className="pr-5">
              Preview
            </Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((r) => (
            <RecRow key={r.track.id} row={r} seed={seed} ticked={ticks.has(r.track.id)} selected={r.track.id === selectedId} onSelect={onSelect} onTick={onTick} />
          ))}
        </TBody>
      </Table>
    </TableScroll>
  );
}

const MOVE_CHIP = /^(same key|key clash|key \?|relative|[+-]1|\+2 boost|\+7 boost|diagonal|clash)$/i;
const PITCH_CHIP = /(% pitch|^same tempo)$/i;

/** Rule verdict from rule_reason ("5A ✗  128 ✓" / "1B ✓  65→130 ✓ (double-time)"). */
function ruleFail(reason: string): string | null {
  const [key = "", ...rest] = reason.split(/\s{2,}/);
  const bpm = rest.join(" ");
  const k = key.includes("✗");
  const b = bpm.includes("✗");
  return k && b ? "key + BPM" : k ? "key" : b ? "BPM" : null;
}

const RecRow = memo(function RecRow({
  row,
  seed,
  ticked,
  selected,
  onSelect,
  onTick,
}: {
  row: CrateRow;
  seed: Track;
  ticked: boolean;
  selected: boolean;
  onSelect: (id: string) => void;
  onTick: (id: string, value: boolean, e: MouseEvent | KeyboardEvent) => void;
}) {
  const { track: t, rec, isSeed, existingPath } = row;
  const passed = isSeed || !!rec?.score.passed;
  const pitch = isSeed ? null : pitchVsSeed(seed.bpm, t.bpm);
  const chips = (rec?.score.chips ?? []).filter((c, i) => !(i === 0 && MOVE_CHIP.test(c)) && !PITCH_CHIP.test(c));
  const fail = rec && !rec.score.passed ? ruleFail(rec.rule_reason) : null;
  const energyDelta = isSeed ? 0 : t.energy - seed.energy;

  return (
    <Tr
      data-row-id={t.id}
      rowHeight={38}
      selected={selected}
      active={isSeed}
      dim={!passed}
      onClick={() => onSelect(t.id)}
      onDoubleClick={(e) => {
        if ((e.target as HTMLElement).closest("button,[role=checkbox],svg")) return;
        onTick(t.id, !ticked, e);
      }}
      className={cn(isSeed && "[&>td]:border-b-line-strong", ticked && !passed && !selected && "[&>td:first-child]:shadow-[inset_2px_0_0_var(--color-warn)]")}
    >
      <Td className="pl-5">
        <Checkbox
          checked={ticked}
          onChange={(v, e) => onTick(t.id, v, e)}
          color={ticked && !passed ? "var(--color-warn)" : undefined}
          title={ticked && !passed ? "Ticked although it fails the key/BPM rule. X to untick" : ticked ? "Untick (X)" : "Tick (X)"}
        />
      </Td>
      <Td className="px-1">
        <PreviewButton track={t} />
      </Td>
      <Td className="px-1">
        <CoverArt src={t.cover_url} size={28} rounded="xs" camelot={t.camelot} />
      </Td>
      <Td truncate className="pr-3">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className={cn("truncate font-medium", passed ? "text-fg" : "text-fg-muted")} title={t.name}>
            {t.name}
          </span>
          {isSeed && (
            <Badge size="xs" tone="accent" variant="solid" className="tracking-wide">
              SEED
            </Badge>
          )}
          {existingPath && (
            <Tooltip content={<span className="select-text">Already in this crate · {existingPath.split("/").pop()}</span>}>
              <Badge size="xs" tone="ok">
                in crate
              </Badge>
            </Tooltip>
          )}
        </div>
        <div className="truncate text-xs text-fg-muted" title={t.artists.join(", ")}>
          {t.artists.join(", ")}
        </div>
      </Td>
      <Td align="center">
        {rec ? (
          <Tooltip content={<span className="num">{`${rec.score.passed ? "Passes" : "Fails"} the rule · ${rec.rule_reason.replace(/\s{2,}/g, " · ")}`}</span>}>
            <ScoreChip value={rec.score.value} move={rec.score.move} passed={rec.score.passed} />
          </Tooltip>
        ) : (
          <span className="text-2xs text-fg-faint">—</span>
        )}
      </Td>
      <Td>{rec ? <MoveChip move={rec.score.move} /> : <span className="text-xs text-fg-subtle">seed</span>}</Td>
      <Td align="center">
        <KeyBadge camelot={t.camelot} keyName={t.key} dim={!passed} />
      </Td>
      <Td align="right" num className={passed ? "text-fg" : undefined}>
        {fmtBpm(t.bpm)}
      </Td>
      <Td align="right" num className={cn("text-xs", pitch ? pitchTone(pitch.pct) : "text-fg-faint")}>
        {pitch ? (
          <span className="inline-flex items-center justify-end gap-1">
            {pitch.factor !== 1 && (
              <Tooltip content={pitch.factor === 2 ? "Half-time: plays at double speed against the seed" : "Double-time: plays at half speed against the seed"}>
                <span className="rounded-xs bg-raised px-0.5 text-2xs text-fg-muted">{pitch.factor === 2 ? "½×" : "2×"}</span>
              </Tooltip>
            )}
            {fmtPitch(pitch.pct)}
          </span>
        ) : (
          "—"
        )}
      </Td>
      <Td align="right" num>
        <EnergyCell energy={t.energy} delta={isSeed ? null : energyDelta} />
      </Td>
      <Td align="right" num muted>
        {fmtTime(t.duration_s)}
      </Td>
      <Td className="pl-3">
        <span className="flex min-w-0 items-center gap-1 overflow-hidden">
          {fail && <span className="h-4 shrink-0 rounded-xs bg-danger/12 px-1 text-2xs leading-4 font-medium whitespace-nowrap text-danger/90">✗ {fail}</span>}
          {chips.length ? <ReasonList chips={chips} /> : !fail && <span className="text-2xs text-fg-faint">{isSeed ? "your pick" : "—"}</span>}
        </span>
      </Td>
      <Td className="pr-5">
        <PreviewWave track={t} bars={34} height={18} />
      </Td>
    </Tr>
  );
});

function ReasonList({ chips }: { chips: string[] }) {
  return (
    <Tooltip content={chips.join(" · ")} className="min-w-0">
      <span className="flex min-w-0 items-center gap-1 overflow-hidden">
        {chips.map((c) => (
          <span key={c} className="h-4 shrink-0 rounded-xs bg-raised px-1 text-2xs leading-4 whitespace-nowrap text-fg-muted num">
            {c}
          </span>
        ))}
      </span>
    </Tooltip>
  );
}

function EnergyCell({ energy, delta }: { energy: number; delta: number | null }) {
  const v = Math.round((energy || 0) * 100);
  const d = delta == null ? null : Math.round(delta * 100);
  return (
    <Tooltip content={d == null ? `Energy ${v}/100` : `Energy ${v}/100 (${d > 0 ? "+" : ""}${d} vs seed)`}>
      <span className="inline-flex items-center gap-1.5">
        <span className="relative hidden h-1 w-6 overflow-hidden rounded-full bg-line-strong/70 min-[1400px]:block">
          <span className="absolute inset-y-0 left-0 rounded-full bg-fg-subtle" style={{ width: `${v}%` }} />
        </span>
        <span className="w-5 text-right">{v}</span>
      </span>
    </Tooltip>
  );
}
