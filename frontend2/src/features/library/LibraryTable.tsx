/**
 * The library table (#73): virtualised rows (only the ones in view are in the DOM) and one canvas, laid over the
 * waveform column, that draws every visible row's overview waveform. Scrolling redraws that canvas from cached
 * per-row images on the next frame: no element, effect or React render per waveform (PERFORMANCE.md).
 *
 * Rows drag (the selection, or the row grabbed) as `application/x-seedbox-tracks`: a JSON list of hashes, which the
 * crate and set list takes. Click selects, ⇧-click a range, ⌘/Ctrl-click toggles.
 *
 * A track that can't go into a set or mix yet (#100) is dimmed, with its readiness badge over its waveform. It still drags
 * (onto a crate); a drag of unready tracks says so (UNREADY_MIME), and the sets refuse them with the reason.
 */
import { AlertTriangle, AudioLines, Check, Clock, Layers, Loader2 } from "lucide-react";
import { memo, type DragEvent, type MouseEvent, type Ref, useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { apiUrl } from "@/api/client";
import type { LibraryRow } from "@/api/types";
import { CoverArt, KeyBadge, Skeleton, Table, TableScroll, TBody, Td, Th, THead, Tooltip, Tr, VirtualRows, waveBars, WaveSkeleton } from "@/components/ui";
import { useFrame } from "@/features/waveform/hooks";
import { formatKey } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { fmtTime } from "@/lib/format";
import { unusableRows } from "@/store/library";
import { ui, useUi } from "@/store/ui";
import { energy10, type Sort, type SortKey } from "./filter";
import { overviews } from "./overviews";
import { readiness } from "./readiness";
import { ReadinessBadge, useReadiness } from "./ReadinessBadge";

export const LIBRARY_ROW_H = 34;
const HINT_SOURCE: Record<string, string> = { rekordbox: "Rekordbox", tunebat: "Tunebat", file: "the file's tags", itunes: "iTunes" };

function keyTitle(r: LibraryRow): string | undefined {
  const hint = r.key_hint ? `${HINT_SOURCE[r.key_hint_from ?? ""] ?? "A tag"} says ${r.key_hint}` : "";
  if (r.key_from === "tags") return "From the file's tags or another app: a hint until our analysis";
  if (r.key_from === "unknown") return `Our analysis isn't sure of the key. ${hint}`.trim();
  if (r.key_from === "user") return `Your own key. ${hint}`.trim();
  return hint || undefined;
}

/** Ours (or "?"), and the hint that disagrees with it. A click opens the key editor. */
function KeyCell({ row: r }: { row: LibraryRow }) {
  const notation = useUi((s) => s.keyNotation);
  return (
    <button
      type="button"
      aria-label="Edit the key"
      className="inline-flex items-center gap-1 align-middle"
      onClick={(e) => {
        e.stopPropagation();
        ui.editKey(r.hash);
      }}
    >
      <KeyBadge camelot={r.key} size="xs" dim={r.key_from === "tags" || r.key_from === "unknown"} />
      {r.key_hint && <span className="text-2xs text-fg-subtle italic">{formatKey(r.key_hint, notation)}</span>}
    </button>
  );
}

export const TRACKS_MIME = "application/x-seedbox-tracks";
/** Also on a drag whose tracks aren't ready for sets and mixes: "all" or "some" of them (a drop target can only see
 * the types while dragging, not the data). */
export const UNREADY_ALL_MIME = "application/x-seedbox-unready-all";
export const UNREADY_SOME_MIME = "application/x-seedbox-unready-some";
const WAVE_PAD_X = 8;
const WAVE_PAD_Y = 6;
const COLS = 11;

export interface LibraryTableProps {
  rows: LibraryRow[];
  sort: Sort;
  onSort: (key: SortKey) => void;
  selected: ReadonlySet<string>;
  onSelect: (hash: string, index: number, e: MouseEvent) => void;
  /** The hashes a drag starting on this row carries. */
  dragHashes: (hash: string) => string[];
  className?: string;
}

export function LibraryTable({ rows, sort, onSort, selected, onSelect, dragHashes, className }: LibraryTableProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const waveTh = useRef<HTMLTableCellElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  // ---- the shared waveform canvas: drawn from refs on the next frame after a scroll, resize or new data
  const draw = useCallback(() => {
    const scroller = scrollRef.current;
    const th = waveTh.current;
    const canvas = canvasRef.current;
    if (!scroller || !th || !canvas) return;
    // the canvas covers the waveform column only (below the header): less to clear and composite each frame
    const headH = (th.parentElement as HTMLElement | null)?.offsetHeight ?? th.offsetHeight;
    const colX = th.offsetLeft - scroller.scrollLeft;
    const colW = th.offsetWidth;
    const viewH = Math.max(0, scroller.clientHeight - headH);
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    if (canvas.width !== Math.round(colW * dpr) || canvas.height !== Math.round(viewH * dpr)) {
      canvas.width = Math.round(colW * dpr);
      canvas.height = Math.round(viewH * dpr);
      canvas.style.width = `${colW}px`;
      canvas.style.height = `${viewH}px`;
    }
    canvas.style.transform = `translate(${colX}px, ${headH}px)`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, colW, viewH);
    const w = Math.floor(colW - 2 * WAVE_PAD_X);
    const h = LIBRARY_ROW_H - 2 * WAVE_PAD_Y;
    if (w < 8) return;
    const top = scroller.scrollTop;
    const list = rowsRef.current;
    const first = Math.max(0, Math.floor(top / LIBRARY_ROW_H));
    const last = Math.min(list.length - 1, Math.ceil((top + viewH) / LIBRARY_ROW_H));
    ctx.save();
    for (let i = first; i <= last; i++) {
      const img = overviews.image(list[i].hash, w, h, dpr);
      const y = i * LIBRARY_ROW_H - top + WAVE_PAD_Y;
      const state = list[i].status.state;
      const outline = img || (state !== "analysing" && state !== "queued") ? null : waveOutline(w, h, dpr, state === "analysing" ? "rgba(111,125,255,0.4)" : "rgba(127,127,127,0.2)");
      ctx.globalAlpha = readiness(list[i]).usable ? 1 : 0.45;
      if (img) ctx.drawImage(img, WAVE_PAD_X, y, w, h);
      else if (outline) ctx.drawImage(outline, WAVE_PAD_X, y, w, h);
      else {
        // nothing coming, or it failed: a faint centre line where the waveform would be
        ctx.fillStyle = state === "failed" ? "rgba(255,77,106,0.45)" : "rgba(127,127,127,0.18)";
        ctx.fillRect(WAVE_PAD_X, y + h / 2 - 0.5, w, 1);
      }
    }
    ctx.restore();
  }, []);
  const redraw = useFrame(draw);

  useEffect(() => overviews.subscribe(redraw), [redraw]);
  useLayoutEffect(redraw, [rows, redraw]);
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    scroller.addEventListener("scroll", redraw, { passive: true });
    const ro = new ResizeObserver(redraw);
    ro.observe(scroller);
    if (waveTh.current) ro.observe(waveTh.current);
    return () => {
      scroller.removeEventListener("scroll", redraw);
      ro.disconnect();
    };
  }, [redraw]);

  return (
    <div className={cn("relative min-h-0", className)}>
      <TableScroll ref={scrollRef} className="h-full" aria-label="Tracks" data-library-table>
        <Table className="table-fixed">
          <THead>
            <LibraryHead sort={sort} onSort={onSort} waveRef={waveTh} />
          </THead>
          <TBody>
            <VirtualRows
              scrollRef={scrollRef}
              count={rows.length}
              colSpan={COLS}
              rowHeight={LIBRARY_ROW_H}
              rowKey={(i) => rows[i].hash}
              row={(i) => <Row row={rows[i]} index={i} selected={selected.has(rows[i].hash)} onSelect={onSelect} dragHashes={dragHashes} />}
            />
          </TBody>
        </Table>
      </TableScroll>
      <canvas ref={canvasRef} aria-hidden className="pointer-events-none absolute top-0 left-0" />
    </div>
  );
}

function LibraryHead({ sort, onSort, waveRef }: { sort?: Sort; onSort?: (key: SortKey) => void; waveRef?: Ref<HTMLTableCellElement> }) {
  const th = (key: SortKey) => (onSort ? { sort: sort?.key === key ? sort.dir : null, onSort: () => onSort(key) } : {});
  return (
    <Tr interactive={false}>
              <Th width={42} aria-label="Artwork" />
              <Th {...th("title")}>Title</Th>
              <Th width="16%" {...th("artist")}>
                Artist
              </Th>
              <Th align="right" width={62} {...th("bpm")}>
                BPM
              </Th>
              <Th align="center" width={84} {...th("key")}>
                Key
              </Th>
              <Th width={74} {...th("energy")} title="Energy, 1-10">
                Energy
              </Th>
              <Th align="right" width={58} {...th("duration")}>
                Time
              </Th>
              <Th width="10%" {...th("genre")}>
                Genre
              </Th>
              <Th ref={waveRef} width="22%">
                Waveform
              </Th>
              <Th align="center" width={64} {...th("status")} title="Analysis and stems">
                Status
              </Th>
              <Th width={4} aria-hidden />
            </Tr>
  );
}

const SKELETON_ROWS = 24;
const WIDTHS = ["w-2/3", "w-1/2", "w-4/5", "w-3/5", "w-3/4"];

/** The table's outline while the library loads: the same columns, and rows shaped like tracks. */
export function LibrarySkeleton({ className }: { className?: string }) {
  return (
    <div className={cn("min-h-0 overflow-hidden", className)} role="status" aria-label="Loading the library">
      <Table className="table-fixed">
        <THead>
          <LibraryHead />
        </THead>
        <TBody>
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <Tr key={i} rowHeight={LIBRARY_ROW_H} interactive={false} aria-hidden>
              <Td className="pr-0">
                <Skeleton className="size-6.5" />
              </Td>
              <Td>
                <Skeleton className={WIDTHS[i % WIDTHS.length]} />
              </Td>
              <Td>
                <Skeleton className={WIDTHS[(i + 2) % WIDTHS.length]} />
              </Td>
              <Td>
                <Skeleton className="ml-auto w-7" />
              </Td>
              <Td>
                <Skeleton className="mx-auto h-4 w-7" />
              </Td>
              <Td>
                <Skeleton className="w-10" />
              </Td>
              <Td>
                <Skeleton className="ml-auto w-8" />
              </Td>
              <Td>
                <Skeleton className={WIDTHS[(i + 1) % WIDTHS.length]} />
              </Td>
              <Td className="py-1.5">
                <div className="h-5">
                  <WaveSkeleton seed={i + 1} bars={64} />
                </div>
              </Td>
              <Td>
                <Skeleton className="mx-auto w-8" />
              </Td>
              <Td />
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

/** A track's waveform outline for the shared canvas, one per size and colour: what is waiting or being analysed. */
const outlines = new Map<string, HTMLCanvasElement>();
function waveOutline(width: number, height: number, dpr: number, color: string): HTMLCanvasElement | null {
  const key = `${width}x${height}x${dpr}${color}`;
  const hit = outlines.get(key);
  if (hit) return hit;
  const c = document.createElement("canvas");
  c.width = Math.round(width * dpr);
  c.height = Math.round(height * dpr);
  const ctx = c.getContext("2d");
  if (!ctx) return null;
  ctx.scale(dpr, dpr);
  ctx.fillStyle = color;
  const bars = waveBars(7, Math.floor(width / 3));
  bars.forEach((v, i) => {
    const h = Math.max(1, v * height);
    ctx.fillRect(i * 3, (height - h) / 2, 2, h);
  });
  outlines.set(key, c);
  return c;
}

interface RowProps {
  row: LibraryRow;
  index: number;
  selected: boolean;
  onSelect: (hash: string, index: number, e: MouseEvent) => void;
  dragHashes: (hash: string) => string[];
}

const Row = memo(function Row({ row: r, index, selected, onSelect, dragHashes }: RowProps) {
  const onDragStart = (e: DragEvent<HTMLTableRowElement>) => {
    const hashes = dragHashes(r.hash);
    e.dataTransfer.effectAllowed = "copy";
    e.dataTransfer.setData(TRACKS_MIME, JSON.stringify(hashes));
    e.dataTransfer.setData("text/plain", hashes.length === 1 ? `${r.artist} - ${r.title}` : `${hashes.length} tracks`);
    const unready = unusableRows(hashes).length;
    if (unready) e.dataTransfer.setData(unready === hashes.length ? UNREADY_ALL_MIME : UNREADY_SOME_MIME, String(unready));
    const ghost = document.createElement("div");
    ghost.textContent = (hashes.length === 1 ? r.title || "1 track" : `${hashes.length} tracks`) + (unready === hashes.length ? " · not ready: crates only" : unready ? ` · ${unready} not ready` : "");
    ghost.className = "fixed -top-20 left-0 rounded-sm bg-accent px-2 py-1 text-xs font-semibold text-accent-fg shadow-lg";
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, -8, -8);
    setTimeout(() => ghost.remove(), 0);
  };
  const tagHint = "From the file's tags or another app: a hint until our analysis";
  // values on their way: the outline of the value, pulsing while it is worked on
  const coming = r.status.state === "analysing" || r.status.state === "queued";
  const waiting = r.status.state === "queued";
  const { usable } = useReadiness(r);
  return (
    <Tr
      rowHeight={LIBRARY_ROW_H}
      selected={selected}
      draggable
      onDragStart={onDragStart}
      onClick={(e) => onSelect(r.hash, index, e)}
      data-hash={r.hash}
      data-unready={usable ? undefined : ""}
      className={cn(!usable && "[&>td:not([data-keep])]:opacity-45")}
    >
      <Td className="pr-0">
        <CoverArt src={r.artwork ? apiUrl(`/api/tracks/${r.hash}/artwork`) : null} size={26} camelot={r.key ?? undefined} rounded="xs" />
      </Td>
      <Td truncate className="font-medium" title={r.album ? `${r.title} · ${r.album}` : r.title}>
        {r.title}
      </Td>
      <Td truncate muted>
        {r.artist}
      </Td>
      <Td num align="right" className={cn(r.bpm_from === "tags" && "text-fg-subtle italic")} title={r.bpm_from === "tags" ? tagHint : undefined}>
        {r.bpm ? (Number.isInteger(r.bpm) ? r.bpm : r.bpm.toFixed(1)) : coming && <Skeleton still={waiting} className="ml-auto w-7" />}
      </Td>
      <Td align="center" title={keyTitle(r)}>
        {r.key || r.key_from ? <KeyCell row={r} /> : coming && <Skeleton still={waiting} className="mx-auto h-4 w-7" />}
      </Td>
      <Td>
        {r.energy === null && coming ? <Skeleton still={waiting} className="w-10" /> : <EnergyMeter energy={r.energy} hint={r.energy_from === "tags"} />}
      </Td>
      <Td num align="right" muted>
        {fmtTime(r.duration)}
      </Td>
      <Td truncate muted>
        {r.genre}
      </Td>
      <Td className="max-w-0 overflow-hidden" data-keep>
        {/* over the (dimmed) waveform, where the analysis shows: above the shared canvas, below the sticky header */}
        <ReadinessBadge row={r} className="relative z-raised rounded-xs bg-bg pr-0.5" />
      </Td>
      <Td align="center">
        <StatusIcons row={r} />
      </Td>
      <Td />
    </Tr>
  );
});

function EnergyMeter({ energy, hint }: { energy: number | null; hint: boolean }) {
  const e = energy10(energy);
  if (e === null) return null;
  return (
    <span className="flex items-center gap-1.5" title={`Energy ${e}/10${hint ? " (from tags)" : ""}`}>
      <span className="flex h-2.5 items-end gap-px" aria-hidden>
        {Array.from({ length: 5 }, (_, k) => (
          <span key={k} className={cn("w-1 rounded-xs", k < Math.ceil(e / 2) ? (hint ? "bg-fg-subtle" : "bg-accent") : "bg-line-strong")} style={{ height: `${40 + k * 15}%` }} />
        ))}
      </span>
      <span className={cn("num text-xs", hint ? "text-fg-subtle" : "text-fg-muted")}>{e}</span>
    </span>
  );
}

function StatusIcons({ row }: { row: LibraryRow }) {
  const s = row.status;
  const analysis =
    s.state === "analysing" ? (
      <Loader2 size={13} className="animate-spin text-accent" />
    ) : s.state === "queued" ? (
      <Clock size={13} className="text-fg-subtle" />
    ) : s.state === "failed" ? (
      <AlertTriangle size={13} className="text-danger" />
    ) : s.state === "done" ? (
      <Check size={13} className="text-ok" />
    ) : (
      <AudioLines size={13} className="text-fg-faint" />
    );
  const label = { analysing: "Analysing", queued: "Waiting to be analysed", failed: "Analysis failed", done: "Analysed", new: "Not analysed yet" }[s.state];
  const jobs = Object.entries(s.jobs)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
  const stems =
    s.stems === true ? "Stems ready" : s.stems === "running" ? `Separating stems ${Math.round((s.stems_progress ?? 0) * 100)}%` : s.stems === "queued" ? "Stems queued" : s.stems === "failed" ? "Stem separation failed" : "No stems yet";
  return (
    <Tooltip content={`${label}${jobs ? ` (${jobs})` : ""} · ${stems}`}>
      <span className="inline-flex items-center gap-1.5" aria-label={`${label}. ${stems}`}>
        {analysis}
        <Layers size={13} className={cn(s.stems === true ? "text-fg-muted" : s.stems === "running" ? "animate-pulse-soft text-accent" : s.stems === "failed" ? "text-danger" : "text-fg-faint/60")} />
      </span>
    </Tooltip>
  );
}
