/**
 * Dense table primitives (30px rows, sticky header). Compose:
 *   <TableScroll><Table><THead><Tr><Th sort="desc" onSort={…}>Score</Th></Tr></THead>
 *   <TBody><Tr selected={…} dim={!passed} onClick={…}><Td num align="right">128</Td></Tr></TBody></Table></TableScroll>
 */
import { ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react";
import type { HTMLAttributes, ReactNode, Ref, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

/** Scroll container; the header sticks to its top. Give it a height (flex-1 / h-full). */
export function TableScroll({ className, children, ref, ...rest }: HTMLAttributes<HTMLDivElement> & { ref?: Ref<HTMLDivElement> }) {
  return (
    <div ref={ref} className={cn("relative min-h-0 overflow-auto scroll-thin", className)} {...rest}>
      {children}
    </div>
  );
}

export function Table({ className, children, ...rest }: HTMLAttributes<HTMLTableElement>) {
  return (
    <table className={cn("w-full border-separate border-spacing-0 text-sm", className)} {...rest}>
      {children}
    </table>
  );
}

export function THead({ children, className }: { children: ReactNode; className?: string }) {
  return <thead className={className}>{children}</thead>;
}

export function TBody({ children, className }: { children: ReactNode; className?: string }) {
  return <tbody className={className}>{children}</tbody>;
}

export interface TrProps extends HTMLAttributes<HTMLTableRowElement> {
  selected?: boolean;
  /** Dimmed row (failed rule / skipped). */
  dim?: boolean;
  /** Highlight (e.g. the seed row, now playing). */
  active?: boolean;
  interactive?: boolean;
  /** Row height in px (default 30 — dense). */
  rowHeight?: number;
  ref?: Ref<HTMLTableRowElement>;
}

export function Tr({ selected, dim, active, interactive = true, rowHeight = 30, className, children, ref, style, ...rest }: TrProps) {
  return (
    <tr
      ref={ref}
      style={{ height: rowHeight, ...style }}
      aria-selected={selected || undefined}
      data-selected={selected || undefined}
      className={cn(
        "group/row",
        interactive && "hover:[&>td]:bg-hover",
        active && "[&>td]:bg-accent/[0.06]",
        selected && "[&>td]:!bg-selected [&>td:first-child]:shadow-selected-edge",
        dim && "[&>td]:text-fg-subtle",
        className,
      )}
      {...rest}
    >
      {children}
    </tr>
  );
}

export type SortDir = "asc" | "desc" | null;
type Align = "left" | "right" | "center";

export interface ThProps extends Omit<ThHTMLAttributes<HTMLTableCellElement>, "align"> {
  align?: Align;
  /** Current sort direction for this column (null = sortable but unsorted). Omit for non-sortable. */
  sort?: SortDir;
  onSort?: () => void;
  width?: number | string;
}

export function Th({ align = "left", sort, onSort, width, className, children, style, ...rest }: ThProps) {
  const sortable = onSort !== undefined;
  const Icon = sort === "asc" ? ChevronUp : sort === "desc" ? ChevronDown : ChevronsUpDown;
  return (
    <th
      style={{ width, ...style }}
      aria-sort={sort === "asc" ? "ascending" : sort === "desc" ? "descending" : undefined}
      className={cn(
        "group/th sticky top-0 z-sticky h-7 border-b border-line bg-panel px-2 text-2xs font-semibold tracking-caps whitespace-nowrap text-fg-subtle uppercase",
        align === "right" && "text-right",
        align === "center" && "text-center",
        align === "left" && "text-left",
        sortable && "cursor-default hover:text-fg-muted",
        sort && "text-fg-muted",
        className,
      )}
      onClick={onSort}
      {...rest}
    >
      <span className={cn("inline-flex items-center gap-0.5", align === "right" && "flex-row-reverse")}>
        {children}
        {sortable && <Icon size={11} className={cn(!sort && "opacity-0 group-hover/th:opacity-40")} />}
      </span>
    </th>
  );
}

export interface TdProps extends Omit<TdHTMLAttributes<HTMLTableCellElement>, "align"> {
  align?: Align;
  /** Tabular numerals (BPM, time, score). */
  num?: boolean;
  /** Muted secondary text. */
  muted?: boolean;
  truncate?: boolean;
}

export function Td({ align = "left", num, muted, truncate, className, children, ...rest }: TdProps) {
  return (
    <td
      className={cn(
        "border-b border-line/60 bg-surface px-2 align-middle transition-colors duration-fast",
        align === "right" && "text-right",
        align === "center" && "text-center",
        num && "num",
        muted && "text-fg-muted",
        truncate && "max-w-0 truncate",
        className,
      )}
      {...rest}
    >
      {children}
    </td>
  );
}

/** Sort helper for tables: toggles desc → asc → desc on the same key, new keys start at `firstDir`. */
export function nextSort<K extends string>(current: { key: K; dir: "asc" | "desc" }, key: K, firstDir: "asc" | "desc" = "desc") {
  return current.key === key ? { key, dir: current.dir === "desc" ? ("asc" as const) : ("desc" as const) } : { key, dir: firstDir };
}
