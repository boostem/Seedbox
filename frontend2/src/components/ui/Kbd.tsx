import type { ReactNode } from "react";
import { comboLabel } from "@/lib/hotkeys";
import { cn } from "@/lib/cn";

/** Keyboard key cap. `combo="Mod+F"` renders "⌘F"; or pass children. */
export function Kbd({ combo, children, className, subtle }: { combo?: string; children?: ReactNode; className?: string; subtle?: boolean }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-xs border px-1 font-sans text-2xs font-medium num",
        subtle ? "border-line-strong bg-bg/40 text-fg-subtle" : "border-white/25 bg-white/10 text-current",
        className,
      )}
    >
      {combo ? comboLabel(combo) : children}
    </kbd>
  );
}
