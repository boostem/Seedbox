import type { CSSProperties } from "react";
import { cn } from "@/lib/cn";

export interface SliderProps {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  /** Fill colour (CSS value). */
  color?: string;
  className?: string;
  label?: string;
  onCommit?: (v: number) => void;
}

/** Horizontal range slider (volume, tolerances). onChange fires while dragging; onCommit on release. */
export function Slider({ value, onChange, min = 0, max = 1, step = 0.01, disabled, color, className, label, onCommit }: SliderProps) {
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
  return (
    <input
      type="range"
      aria-label={label}
      className={cn("sb-range", className)}
      style={{ "--sb-pct": `${pct}%`, ...(color ? { "--sb-fill": color } : {}) } as CSSProperties}
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(Number(e.target.value))}
      onPointerUp={(e) => onCommit?.(Number((e.target as HTMLInputElement).value))}
      onKeyUp={(e) => onCommit?.(Number((e.target as HTMLInputElement).value))}
    />
  );
}
