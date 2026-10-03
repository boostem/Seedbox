import { player } from "@/audio/player";
import { focusSearch } from "@/lib/focusSearch";
import { useHotkey } from "@/lib/hotkeys";
import { navigate } from "@/lib/router";

/** App-wide shortcuts. Pages add their own with useHotkey (later registrations win). */
export function GlobalHotkeys() {
  useHotkey("Space", () => player.toggleSelection());
  useHotkey("Mod+k", (e) => (e.shiftKey ? false : focusSearch()), { allowInInputs: true }); // works while typing; pauses while a Modal is open
  useHotkey("Mod+1", () => navigate({ name: "search" }));
  useHotkey("Mod+2", () => navigate({ name: "crates" }));
  useHotkey("Mod+,", () => navigate({ name: "settings" }));
  useHotkey("Mod+[", () => history.back());
  useHotkey("Mod+]", () => history.forward());
  useHotkey("Mod+.", () => player.stop());
  return null;
}
