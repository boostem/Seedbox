import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: ReactNode;
  description?: ReactNode;
  /** Buttons. */
  action?: ReactNode;
  compact?: boolean;
  className?: string;
}

/** Centered placeholder for empty lists, errors and "nothing selected". */
export function EmptyState({ icon: Icon, title, description, action, compact, className }: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "gap-1.5 py-6" : "gap-2.5 py-16", className)}>
      {Icon && (
        <div className={cn("grid place-items-center rounded-lg border border-line bg-raised/60 text-fg-subtle", compact ? "size-8" : "size-11")}>
          <Icon size={compact ? 16 : 20} strokeWidth={1.75} />
        </div>
      )}
      <div className={cn("font-medium text-fg", compact ? "text-sm" : "text-md")}>{title}</div>
      {description && <div className={cn("max-w-[420px] text-fg-subtle", compact ? "text-xs" : "text-sm")}>{description}</div>}
      {action && <div className="mt-1.5 flex items-center gap-2">{action}</div>}
    </div>
  );
}
