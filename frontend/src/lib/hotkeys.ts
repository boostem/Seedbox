/**
 * Keyboard shortcuts. `useHotkey("Space", fn)`; combos: "Mod+F" (⌘ on mac), "Shift+ArrowDown", "Escape", "Enter",
 * "Delete", "1". Handlers are skipped while typing in inputs unless `allowInInputs`. Later-mounted handlers for the
 * same combo run first and may call `e.stopImmediatePropagation()` (return true from the handler does it for you).
 */
import { useEffect, useRef } from "react";

export const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

export interface HotkeyOptions {
  enabled?: boolean;
  allowInInputs?: boolean;
  preventDefault?: boolean; // default true
  allowInModal?: boolean; // default false: shortcuts pause while a <Modal> is open
}

export function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  if (t.isContentEditable) return true;
  const tag = t.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = (t as HTMLInputElement).type;
    return !["checkbox", "radio", "range", "button", "submit"].includes(type);
  }
  return false;
}

export function matchesCombo(e: KeyboardEvent, combo: string): boolean {
  const parts = combo.split("+");
  const key = parts.pop() ?? "";
  const mods = new Set(parts.map((p) => p.toLowerCase()));
  const wantMeta = mods.has("meta") || (mods.has("mod") && IS_MAC);
  const wantCtrl = mods.has("ctrl") || (mods.has("mod") && !IS_MAC);
  if (e.metaKey !== wantMeta || e.ctrlKey !== wantCtrl) return false;
  if (e.altKey !== mods.has("alt")) return false;
  // shift is only enforced when asked for or when the key is a named key (letters/symbols vary with shift)
  if (mods.has("shift") !== e.shiftKey && (mods.has("shift") || key.length > 1)) return false;
  if (key === "Space") return e.code === "Space";
  return e.key === key || e.key.toLowerCase() === key.toLowerCase() || e.code === key;
}

const stack: { combo: string; fn: (e: KeyboardEvent) => boolean | void; opts: HotkeyOptions }[] = [];
let installed = false;

function onKeyDown(e: KeyboardEvent) {
  const modalOpen = document.querySelector("dialog[open]") !== null;
  for (let i = stack.length - 1; i >= 0; i--) {
    const h = stack[i];
    if (h.opts.enabled === false) continue;
    if (modalOpen && !h.opts.allowInModal) continue;
    if (!h.opts.allowInInputs && isTypingTarget(e.target)) continue;
    if (!matchesCombo(e, h.combo)) continue;
    if (h.opts.preventDefault !== false) e.preventDefault();
    const handled = h.fn(e);
    if (handled !== false) return; // return false from a handler to let earlier ones run too
  }
}

/** Register a global shortcut for the component's lifetime. */
export function useHotkey(combo: string | string[], handler: (e: KeyboardEvent) => boolean | void, opts: HotkeyOptions = {}): void {
  const ref = useRef(handler);
  ref.current = handler;
  const combos = Array.isArray(combo) ? combo : [combo];
  const key = combos.join("|");
  const { enabled = true, allowInInputs = false, preventDefault = true, allowInModal = false } = opts;
  useEffect(() => {
    if (!installed) {
      window.addEventListener("keydown", onKeyDown);
      installed = true;
    }
    const entries = key.split("|").map((c) => ({
      combo: c,
      fn: (e: KeyboardEvent) => ref.current(e),
      opts: { enabled, allowInInputs, preventDefault, allowInModal },
    }));
    stack.push(...entries);
    return () => {
      for (const en of entries) {
        const i = stack.indexOf(en);
        if (i >= 0) stack.splice(i, 1);
      }
    };
  }, [key, enabled, allowInInputs, preventDefault, allowInModal]);
}

/** Human label for a combo: "Mod+F" → "⌘F" on mac. */
export function comboLabel(combo: string): string {
  const map: Record<string, string> = IS_MAC
    ? { mod: "⌘", meta: "⌘", ctrl: "⌃", alt: "⌥", shift: "⇧" }
    : { mod: "Ctrl+", meta: "Win+", ctrl: "Ctrl+", alt: "Alt+", shift: "Shift+" };
  const keys: Record<string, string> = {
    Space: "Space", ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→", Enter: "↵", Escape: "Esc",
    Backspace: "⌫", Delete: "⌦", Tab: "⇥",
  };
  const parts = combo.split("+");
  const key = parts.pop() ?? "";
  return parts.map((p) => map[p.toLowerCase()] ?? p).join("") + (keys[key] ?? key.toUpperCase());
}
