import { Check, Minus } from "lucide-react";
import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean, e: MouseEvent | KeyboardEvent) => void;
  indeterminate?: boolean;
  disabled?: boolean;
  label?: ReactNode;
  /** Colour when checked (defaults to accent) — e.g. a move colour. */
  color?: string;
  size?: "sm" | "md";
  className?: string;
  title?: string;
}

/**
 * Checkbox (role=checkbox). Clicks don't bubble to the row (so ticking doesn't change selection);
 * shift-click range ticking can read `e.shiftKey` from the onChange event.
 */
/** A mouse click mustn't leave keyboard focus on the box: Space/Enter would then toggle it instead of reaching the
 * page's shortcuts (play the selected row, tick the selected row). Keyboard users who Tab here keep focus. */
function dropMouseFocus(e: MouseEvent) {
  const el = document.activeElement;
  if (e.detail > 0 && el instanceof HTMLElement && e.currentTarget.contains(el)) el.blur();
}

export function Checkbox({ checked, onChange, indeterminate, disabled, label, color, size = "sm", className, title }: CheckboxProps) {
  const on = checked || indeterminate;
  const box = (
    <span
      role="checkbox"
      aria-checked={indeterminate ? "mixed" : checked}
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : 0}
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        dropMouseFocus(e);
        if (!disabled) onChange(!checked, e);
      }}
      onKeyDown={(e) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
          if (!disabled) onChange(!checked, e);
        }
      }}
      style={on && color ? { background: color, borderColor: color } : undefined}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-xs border transition-colors duration-fast",
        size === "sm" ? "size-3.5" : "size-4",
        on ? "border-accent bg-accent text-bg" : "border-fg-faint bg-bg/40 hover:border-fg-subtle",
        disabled && "opacity-40",
        !label && className,
      )}
    >
      {indeterminate ? <Minus size={10} strokeWidth={3.5} /> : checked ? <Check size={10} strokeWidth={3.5} /> : null}
    </span>
  );
  if (!label) return box;
  return (
    <span
      className={cn("inline-flex items-center gap-2 text-sm text-fg-muted", disabled && "opacity-50", className)}
      onClick={(e) => {
        e.stopPropagation();
        dropMouseFocus(e);
        if (!disabled) onChange(!checked, e);
      }}
    >
      {box}
      <span>{label}</span>
    </span>
  );
}
