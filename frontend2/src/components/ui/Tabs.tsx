import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface TabItem<T extends string> {
  id: T;
  label: ReactNode;
  icon?: LucideIcon;
  count?: number;
  disabled?: boolean;
  title?: string; // native tooltip
}

export interface TabsProps<T extends string> {
  items: TabItem<T>[];
  value: T;
  onChange: (id: T) => void;
  /** underline = page sections; segmented = compact toggle group (view modes, in/out). */
  variant?: "underline" | "segmented";
  size?: "sm" | "md";
  className?: string;
}

export function Tabs<T extends string>({ items, value, onChange, variant = "underline", size = "sm", className }: TabsProps<T>) {
  if (variant === "segmented") {
    return (
      <div role="tablist" className={cn("inline-flex items-center gap-px rounded-sm border border-line-strong bg-bg/60 p-px", className)}>
        {items.map(({ id, label, icon: Icon, count, disabled, title }) => (
          <button
            key={id}
            role="tab"
            type="button"
            aria-selected={value === id}
            title={title}
            disabled={disabled}
            onClick={() => onChange(id)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-xs font-medium transition-colors disabled:opacity-40",
              size === "sm" ? "h-control-sm px-2 text-xs" : "h-control-md px-3 text-sm",
              value === id ? "bg-overlay text-fg shadow-card" : "text-fg-subtle hover:text-fg-muted",
            )}
          >
            {Icon && <Icon size={size === "sm" ? 12 : 14} />}
            {label}
            {count != null && <span className="text-fg-subtle num">{count}</span>}
          </button>
        ))}
      </div>
    );
  }
  return (
    <div role="tablist" className={cn("flex items-end gap-4 border-b border-line", className)}>
      {items.map(({ id, label, icon: Icon, count, disabled, title }) => (
        <button
          key={id}
          role="tab"
          type="button"
          aria-selected={value === id}
          title={title}
          disabled={disabled}
          onClick={() => onChange(id)}
          className={cn(
            "relative -mb-px inline-flex items-center gap-1.5 border-b-2 pb-2 font-medium transition-colors disabled:opacity-40",
            size === "sm" ? "text-sm" : "text-base",
            value === id ? "border-accent text-fg" : "border-transparent text-fg-subtle hover:text-fg-muted",
          )}
        >
          {Icon && <Icon size={14} />}
          {label}
          {count != null && <span className={cn("rounded-xs px-1 text-2xs num", value === id ? "bg-accent/20 text-accent-hover" : "bg-raised text-fg-subtle")}>{count}</span>}
        </button>
      ))}
    </div>
  );
}
