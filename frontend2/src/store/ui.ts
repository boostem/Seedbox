/** Shell overlays: the command palette (⌘K) and the keyboard shortcuts sheet (?), one at a time; the track whose key
 * is being edited; and how keys are written (Camelot or Open Key, remembered in this browser). */
import { create } from "zustand";
import type { KeyNotation } from "@/lib/camelot";

const NOTATION_STORE = "seedbox2.keyNotation";

interface UiState {
  paletteOpen: boolean;
  shortcutsOpen: boolean;
  keyEditor: string | null; // a track's hash
  keyNotation: KeyNotation;
}

function savedNotation(): KeyNotation {
  try {
    return localStorage.getItem(NOTATION_STORE) === "open" ? "open" : "camelot";
  } catch {
    return "camelot";
  }
}

export const useUi = create<UiState>()(() => ({ paletteOpen: false, shortcutsOpen: false, keyEditor: null, keyNotation: savedNotation() }));

export const ui = {
  openPalette: () => useUi.setState({ paletteOpen: true, shortcutsOpen: false }),
  closePalette: () => useUi.setState({ paletteOpen: false }),
  togglePalette: () => (useUi.getState().paletteOpen ? ui.closePalette() : ui.openPalette()),
  openShortcuts: () => useUi.setState({ shortcutsOpen: true, paletteOpen: false }),
  closeShortcuts: () => useUi.setState({ shortcutsOpen: false }),
  toggleShortcuts: () => (useUi.getState().shortcutsOpen ? ui.closeShortcuts() : ui.openShortcuts()),
  editKey: (hash: string | null) => useUi.setState({ keyEditor: hash }),
  setKeyNotation: (keyNotation: KeyNotation) => {
    useUi.setState({ keyNotation });
    try {
      localStorage.setItem(NOTATION_STORE, keyNotation);
    } catch {
      // private mode: it lasts until the page reloads
    }
  },
};
