/** Keyboard shortcuts of the mix DAW: ⌫ delete, Esc deselect, ⌘Z / ⌘⇧Z undo / redo, = / − zoom, 0 fit. */
import { useHotkey } from "@/lib/hotkeys";

interface DawHotkeys {
  enabled: boolean;
  /** Deletes the selected keyframes or loop; false when nothing was selected. */
  deleteSelection: () => boolean;
  /** Closes the menu and clears the selections; false when there was nothing to clear. */
  clear: () => boolean;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  zoom: (factor: number) => void;
  fit: () => void;
}

export function useDawHotkeys({ enabled, deleteSelection, clear, undo, redo, canUndo, canRedo, zoom, fit }: DawHotkeys) {
  useHotkey(["Delete", "Backspace"], () => deleteSelection(), { enabled });
  useHotkey("Escape", () => clear(), { enabled });
  useHotkey("Mod+z", () => (canUndo ? undo() : false), { enabled });
  useHotkey("Mod+Shift+z", () => (canRedo ? redo() : false), { enabled });
  useHotkey(["=", "Shift+Equal"], () => zoom(1.5), { enabled });
  useHotkey("-", () => zoom(1 / 1.5), { enabled });
  useHotkey("0", () => fit(), { enabled });
}
