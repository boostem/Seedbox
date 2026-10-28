/** Right-click menu of the mix DAW: a keyframe's curve, or a loop's length; both can delete. Keyboard: arrows, Home/End, Esc. */
import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { Curve, MixPlan } from "@/api/types";
import { cn } from "@/lib/cn";
import type { Menu } from "./daw";
import { CURVES, getLane } from "./lanes";

export function ContextMenu({ menu, plan, onClose, onCurve, onLength, onDelete }: { menu: Menu; plan: MixPlan; onClose: () => void; onCurve: (c: Curve) => void; onLength: (n: number) => void; onDelete: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  // keyboard: focus the checked (or first) item, give focus back to the opener when the menu goes away
  useLayoutEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const items = [...(ref.current?.querySelectorAll<HTMLElement>("[role^=menuitem]") ?? [])];
    (items.find((el) => el.getAttribute("aria-checked") === "true") ?? items[0])?.focus();
    return () => {
      if (opener?.isConnected && (!document.activeElement || document.activeElement === document.body || ref.current?.contains(document.activeElement))) opener.focus();
    };
  }, []);
  useEffect(() => {
    const close = () => onClose();
    window.addEventListener("pointerdown", close);
    window.addEventListener("blur", close);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("blur", close);
    };
  }, [onClose]);
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const items = [...e.currentTarget.querySelectorAll<HTMLElement>("[role^=menuitem]")];
    const at = items.indexOf(document.activeElement as HTMLElement);
    const go = (i: number) => items[(i + items.length) % items.length]?.focus();
    if (e.key === "ArrowDown") go(at + 1);
    else if (e.key === "ArrowUp") go(at < 0 ? -1 : at - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(-1);
    else if (e.key === "Escape" || e.key === "Tab") onClose();
    else {
      // Enter/Space click the focused item; keep them (and everything else) from the page shortcuts (Space = play)
      e.stopPropagation();
      return;
    }
    e.preventDefault();
    e.stopPropagation();
  };
  const current = menu.kind === "kf" ? getLane(plan, menu.key)[menu.index]?.curve : null;
  const loopLen = menu.kind === "loop" ? plan[menu.deck].loops[menu.index]?.length_beats : null;
  const item = "flex h-7 w-full items-center gap-2 rounded-xs px-2 text-left text-sm hover:bg-raised focus-visible:bg-raised";
  const label = menu.kind === "kf" ? "Curve to next key" : "Loop length";
  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      className="fixed z-[1000] w-44 rounded-sm bg-overlay p-1 shadow-pop"
      style={{ left: Math.min(menu.x, window.innerWidth - 190), top: Math.min(menu.y, window.innerHeight - 220) }}
      onPointerDown={(e) => e.stopPropagation()}
      onKeyDown={onKeyDown}
    >
      <div className="px-2 pt-1 pb-0.5 label-caps" aria-hidden>
        {label}
      </div>
      {menu.kind === "kf"
        ? CURVES.map((c) => (
            <button key={c} type="button" role="menuitemradio" aria-checked={current === c} tabIndex={-1} className={item} onClick={() => onCurve(c)}>
              <span className="w-3 text-accent-hover">{current === c ? "✓" : ""}</span>
              <span className="capitalize">{c}</span>
              <span className="ml-auto text-2xs text-fg-subtle">{c === "hold" ? "step" : c === "ease" ? "S-curve" : "ramp"}</span>
            </button>
          ))
        : [1, 2, 4, 8, 16].map((n) => (
            <button key={n} type="button" role="menuitemradio" aria-checked={loopLen === n} tabIndex={-1} className={item} onClick={() => onLength(n)}>
              <span className="w-3 text-accent-hover">{loopLen === n ? "✓" : ""}</span>
              {n} beat{n === 1 ? "" : "s"}
            </button>
          ))}
      <div role="separator" className="my-1 h-px bg-line" />
      <button type="button" role="menuitem" tabIndex={-1} className={cn(item, "text-danger")} onClick={onDelete}>
        <span className="w-3" />
        Delete <span className="ml-auto text-2xs text-fg-subtle">⌫</span>
      </button>
    </div>,
    document.body,
  );
}
