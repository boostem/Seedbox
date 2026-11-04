import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { seedOf, WaveArt } from "./WaveArt";

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: ReactNode;
  description?: ReactNode;
  /** Buttons. */
  action?: ReactNode;
  compact?: boolean;
  /** `danger`: something went wrong; say how to fix it in `description` and offer it in `action`. */
  tone?: "default" | "danger";
  className?: string;
}

/** Centered placeholder for empty lists, errors and "nothing selected": the waveform illustration with the icon on
 * it, or in tight spots (`compact`) just the icon. */
export function EmptyState({ icon: Icon, title, description, action, compact, tone = "default", className }: EmptyStateProps) {
  const danger = tone === "danger";
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "gap-1.5 py-6" : "gap-2.5 py-16", className)} role={danger ? "alert" : undefined}>
      {compact ? (
        Icon && (
          <div className={cn("grid size-8 place-items-center rounded-lg border border-line bg-raised/60", danger ? "text-danger" : "text-fg-subtle")}>
            <Icon size={16} strokeWidth={1.75} />
          </div>
        )
      ) : (
        <div className="relative mb-2 grid place-items-center">
          <WaveArt seed={typeof title === "string" ? seedOf(title) : 1} tone={tone} />
          {Icon && (
            <div className={cn("absolute grid size-11 place-items-center rounded-full border border-line-strong bg-surface shadow-md", danger ? "text-danger" : "text-fg-muted")}>
              <Icon size={20} strokeWidth={1.75} />
            </div>
          )}
        </div>
      )}
      <div className={cn("font-medium text-fg", compact ? "text-sm" : "text-md")}>{title}</div>
      {description && <div className={cn("max-w-md text-fg-subtle", compact ? "text-xs" : "text-sm")}>{description}</div>}
      {action && <div className="mt-1.5 flex items-center gap-2">{action}</div>}
    </div>
  );
}
