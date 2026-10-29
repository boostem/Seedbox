/**
 * Keep something mounted while it animates out. `const p = usePresence(open)`: render while `p.mounted`, put
 * `data-state={p.state}` on the element (its CSS picks the in/out animation from that) and `onAnimationEnd={p.onAnimationEnd}`
 * on it. With reduced motion the animations last 1 ms, so it unmounts at once; a timer covers an animation that
 * never runs (an element that isn't rendered).
 */
import { type AnimationEvent, useEffect, useState } from "react";

const FALLBACK_MS = 500;

export function usePresence(open: boolean): { mounted: boolean; state: "open" | "closed"; onAnimationEnd: (e: AnimationEvent) => void } {
  const [mounted, setMounted] = useState(open);
  if (open && !mounted) setMounted(true);
  useEffect(() => {
    if (open || !mounted) return;
    const t = setTimeout(() => setMounted(false), FALLBACK_MS);
    return () => clearTimeout(t);
  }, [open, mounted]);
  return {
    mounted: mounted || open,
    state: open ? "open" : "closed",
    onAnimationEnd: (e) => {
      if (!open && e.target === e.currentTarget) setMounted(false);
    },
  };
}
