/**
 * Keyboard row navigation for tables/lists: ↑/↓ (and k/j) move the selection, Home/End jump, and the selected
 * row is scrolled into view. Put `data-row-id={id}` on each row element.
 *
 *   useListNav(rows.map((r) => r.track.id), selectedId, (id) => select(id));
 */
import { useEffect } from "react";
import { useHotkey } from "./hotkeys";

export function useListNav(ids: string[], selectedId: string | null, onSelect: (id: string) => void, opts: { enabled?: boolean } = {}): void {
  const enabled = opts.enabled ?? true;
  const move = (delta: number | "start" | "end") => {
    if (!ids.length) return;
    const i = selectedId ? ids.indexOf(selectedId) : -1;
    const next =
      delta === "start" ? 0 : delta === "end" ? ids.length - 1 : i < 0 ? (delta > 0 ? 0 : ids.length - 1) : Math.max(0, Math.min(ids.length - 1, i + delta));
    onSelect(ids[next]);
  };
  useHotkey(["ArrowDown", "j"], () => move(1), { enabled });
  useHotkey(["ArrowUp", "k"], () => move(-1), { enabled });
  useHotkey("Home", () => move("start"), { enabled });
  useHotkey("End", () => move("end"), { enabled });
  useHotkey("PageDown", () => move(10), { enabled });
  useHotkey("PageUp", () => move(-10), { enabled });

  useEffect(() => {
    if (!selectedId) return;
    const el = document.querySelector(`[data-row-id="${CSS.escape(selectedId)}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedId]);
}
