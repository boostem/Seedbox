import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";

export type TooltipSide = "top" | "bottom" | "left" | "right";

export interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: TooltipSide;
  delay?: number;
  /** Wrapper element classes (the trigger is wrapped in an inline-flex span). */
  className?: string;
  disabled?: boolean;
}

/** Hover/focus tooltip rendered in a portal (never clipped by scroll containers). */
export function Tooltip({ content, children, side = "top", delay = 350, className, disabled }: TooltipProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  const show = () => {
    if (disabled || content == null || content === "") return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setOpen(true), delay);
  };
  const hide = () => {
    clearTimeout(timer.current);
    setOpen(false);
    setPos(null);
  };
  useEffect(() => () => clearTimeout(timer.current), []);

  useLayoutEffect(() => {
    if (!open || !ref.current || !tipRef.current) return;
    const r = ref.current.getBoundingClientRect();
    const t = tipRef.current.getBoundingClientRect();
    const gap = 6;
    let x = r.left + r.width / 2 - t.width / 2;
    let y = r.top - t.height - gap;
    if (side === "bottom" || (side === "top" && y < 4)) y = r.bottom + gap;
    if (side === "left") {
      x = r.left - t.width - gap;
      y = r.top + r.height / 2 - t.height / 2;
    } else if (side === "right") {
      x = r.right + gap;
      y = r.top + r.height / 2 - t.height / 2;
    }
    x = Math.max(4, Math.min(x, window.innerWidth - t.width - 4));
    y = Math.max(4, Math.min(y, window.innerHeight - t.height - 4));
    setPos({ x, y });
  }, [open, side]);

  return (
    <span ref={ref} className={cn("inline-flex", className)} onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onMouseDown={hide}>
      {children}
      {open &&
        createPortal(
          <div
            ref={tipRef}
            role="tooltip"
            style={{ left: pos?.x ?? -9999, top: pos?.y ?? -9999 }}
            className="pointer-events-none fixed z-tooltip max-w-xs rounded-sm bg-overlay px-2 py-1 text-xs text-fg shadow-pop"
          >
            {content}
          </div>,
          document.body,
        )}
    </span>
  );
}
