import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

/** Bordered content surface (settings groups, side panels). */
export function Panel({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <section className={cn("rounded-md border border-line bg-surface shadow-card", className)} {...rest}>
      {children}
    </section>
  );
}

/** Panel title row: small caps title, optional description and right-side actions. */
export function PanelHeader({ title, description, actions, className }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <header className={cn("flex items-start gap-3 border-b border-line px-4 py-2.5", className)}>
      <div className="min-w-0 flex-1">
        <h3 className="label-caps !text-fg-muted">{title}</h3>
        {description && <p className="mt-0.5 text-xs text-fg-subtle">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </header>
  );
}

export interface PageHeaderProps {
  title: ReactNode;
  /** Small line under the title (counts, path…). */
  subtitle?: ReactNode;
  /** Left of the title (cover art, icon). */
  leading?: ReactNode;
  /** Right-aligned buttons. */
  actions?: ReactNode;
  /** A toolbar row below (filters, tabs). */
  children?: ReactNode;
  className?: string;
}

/** Top of every page: title row + optional toolbar. Pages scroll below it. */
export function PageHeader({ title, subtitle, leading, actions, children, className }: PageHeaderProps) {
  return (
    <header className={cn("shrink-0 border-b border-line bg-surface px-5 pt-4", children ? "pb-0" : "pb-4", className)}>
      <div className="flex items-center gap-3.5">
        {leading}
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-xl font-semibold tracking-tight">{title}</h1>
          {subtitle && <div className="mt-0.5 truncate text-sm text-fg-muted">{subtitle}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {children && <div className="mt-3">{children}</div>}
    </header>
  );
}

/** Horizontal toolbar row (filters, view toggles) — sits under PageHeader or above a table. */
export function Toolbar({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("flex h-11 shrink-0 items-center gap-2 border-b border-line bg-surface px-5", className)}>{children}</div>;
}

/** Key/value line for info panels ("yt-dlp  2026.09.12"). */
export function InfoRow({ label, children, className }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex min-h-8 items-center gap-4 border-b border-line/60 px-4 py-1.5 last:border-b-0", className)}>
      <div className="w-36 shrink-0 text-sm text-fg-subtle">{label}</div>
      <div className="flex min-w-0 flex-1 items-center gap-2 text-sm">{children}</div>
    </div>
  );
}

/** Vertical divider for toolbars. */
export function Divider({ className }: { className?: string }) {
  return <span className={cn("mx-1 h-4 w-px shrink-0 bg-line-strong", className)} />;
}

/** Full-height page column: <Page><PageHeader …/><PageBody>…</PageBody></Page>. */
export function Page({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("flex h-full min-h-0 flex-col", className)}>{children}</div>;
}

/** Scrollable page content area. `padded` adds the standard 20px gutter. */
export function PageBody({ className, padded = true, children }: { className?: string; padded?: boolean; children: ReactNode }) {
  return <div className={cn("min-h-0 flex-1 overflow-auto scroll-thin", padded && "px-5 py-4", className)}>{children}</div>;
}
