/** Shell overlays: the command palette (⌘K) and the keyboard shortcuts sheet (?). One at a time. */
import { create } from "zustand";

interface UiState {
  paletteOpen: boolean;
  shortcutsOpen: boolean;
}

export const useUi = create<UiState>()(() => ({ paletteOpen: false, shortcutsOpen: false }));

export const ui = {
  openPalette: () => useUi.setState({ paletteOpen: true, shortcutsOpen: false }),
  closePalette: () => useUi.setState({ paletteOpen: false }),
  togglePalette: () => (useUi.getState().paletteOpen ? ui.closePalette() : ui.openPalette()),
  openShortcuts: () => useUi.setState({ shortcutsOpen: true, paletteOpen: false }),
  closeShortcuts: () => useUi.setState({ shortcutsOpen: false }),
  toggleShortcuts: () => (useUi.getState().shortcutsOpen ? ui.closeShortcuts() : ui.openShortcuts()),
};
