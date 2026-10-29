import { type ReactNode, useId } from "react";
import { cn } from "@/lib/cn";

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** Optional label rendered to the right (clicking it toggles too). */
  label?: ReactNode;
  size?: "sm" | "md";
  className?: string;
  id?: string;
}

/** On/off toggle (role=switch) for boolean settings. */
export function Switch({ checked, onChange, disabled, label, size = "md", className, id }: SwitchProps) {
  const track = size === "sm" ? "h-4 w-7" : "h-5 w-9";
  const knob = size === "sm" ? "size-3" : "size-4";
  const shift = size === "sm" ? "translate-x-3" : "translate-x-4";
  const autoId = useId();
  const switchId = id ?? autoId;
  return (
    <span className={cn("inline-flex items-center gap-2.5", disabled && "opacity-45", className)}>
      <button
        id={switchId}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn("relative inline-flex shrink-0 items-center rounded-full p-0.5 transition-colors", track, checked ? "bg-accent" : "bg-line-strong hover:bg-fg-faint")}
      >
        <span className={cn("rounded-full bg-white shadow-knob transition-transform", knob, checked && shift)} />
      </button>
      {label && (
        <label htmlFor={switchId} className={cn("text-sm text-fg", !disabled && "cursor-pointer")}>
          {label}
        </label>
      )}
    </span>
  );
}
