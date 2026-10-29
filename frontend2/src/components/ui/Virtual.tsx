/**
 * Virtualised rows (@tanstack/react-virtual): only the rows in view, plus a few, are in the DOM, so a 10,000-track
 * table scrolls and renders like a 30-row one. Every list or table that can grow past a screenful uses these
 * (PERFORMANCE.md). Rows have a fixed height.
 *
 *   const scrollRef = useRef<HTMLDivElement>(null);
 *   <TableScroll ref={scrollRef} className="h-96"><Table><THead>…</THead>
 *     <TBody><VirtualRows scrollRef={scrollRef} count={tracks.length} colSpan={5} rowKey={(i) => tracks[i].id}
 *       row={(i) => <TrackRow track={tracks[i]} />} /></TBody></Table></TableScroll>
 *
 *   <VirtualList className="h-80" count={jobs.length} itemHeight={36} item={(i) => <JobLine job={jobs[i]} />} />
 *
 * Scrolling re-renders the list only when the visible range changes; keep rows cheap and memoised (React.memo) so a
 * range change only renders the rows that came into view.
 */
import { useVirtualizer } from "@tanstack/react-virtual";
import { Fragment, type Key, type ReactNode, type RefObject, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

export const ROW_H = 30; // Tr's default height

export interface VirtualRowsProps {
  /** The scroll container (TableScroll's ref). */
  scrollRef: RefObject<HTMLElement | null>;
  count: number;
  row: (index: number) => ReactNode;
  /** Stable key per row (an id), so selection and focus survive sorting. Default: the index. */
  rowKey?: (index: number) => Key;
  /** Columns, for the spacer rows. */
  colSpan: number;
  rowHeight?: number;
  /** Rows rendered beyond each edge of the view. */
  overscan?: number;
}

/** The visible rows of a table's body, between two spacer rows that keep the scroll height. */
export function VirtualRows({ scrollRef, count, row, rowKey, colSpan, rowHeight = ROW_H, overscan = 12 }: VirtualRowsProps) {
  // the container is an ancestor, so its ref isn't attached yet when this component's layout effects run: pick it
  // up in a passive effect (one extra render on mount)
  const [scroller, setScroller] = useState<HTMLElement | null>(null);
  useEffect(() => setScroller(scrollRef.current), [scrollRef]);
  const v = useVirtualizer({
    count,
    getScrollElement: () => scroller,
    estimateSize: () => rowHeight,
    ...(rowKey && { getItemKey: rowKey }),
    overscan,
    useFlushSync: false,
  });
  const items = v.getVirtualItems();
  const top = items.length ? items[0].start : 0;
  const bottom = items.length ? v.getTotalSize() - items[items.length - 1].end : 0;
  return (
    <>
      {top > 0 && <tr aria-hidden style={{ height: top }}><td colSpan={colSpan} className="p-0" /></tr>}
      {items.map((it) => <Fragment key={it.key}>{row(it.index)}</Fragment>)}
      {bottom > 0 && <tr aria-hidden style={{ height: bottom }}><td colSpan={colSpan} className="p-0" /></tr>}
    </>
  );
}

export interface VirtualListProps {
  count: number;
  item: (index: number) => ReactNode;
  itemKey?: (index: number) => Key;
  itemHeight: number;
  overscan?: number;
  /** Give it a height (h-80, flex-1). */
  className?: string;
  "aria-label"?: string;
}

/** A scrolling list of fixed-height items. */
export function VirtualList({ count, item, itemKey, itemHeight, overscan = 8, className, ...rest }: VirtualListProps) {
  const ref = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({
    count,
    getScrollElement: () => ref.current,
    estimateSize: () => itemHeight,
    ...(itemKey && { getItemKey: itemKey }),
    overscan,
    useFlushSync: false,
  });
  return (
    <div ref={ref} role="list" className={cn("relative min-h-0 overflow-auto scroll-thin", className)} {...rest}>
      <div className="relative w-full" style={{ height: v.getTotalSize() }}>
        {v.getVirtualItems().map((it) => (
          <div key={it.key} role="listitem" className="absolute inset-x-0 top-0" style={{ height: itemHeight, transform: `translateY(${it.start}px)` }}>
            {item(it.index)}
          </div>
        ))}
      </div>
    </div>
  );
}
