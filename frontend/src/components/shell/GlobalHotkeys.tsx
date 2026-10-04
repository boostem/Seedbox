import { player } from "@/audio/player";
import { isSetbox } from "@/lib/appMode";
import { focusSearch } from "@/lib/focusSearch";
import { useHotkey } from "@/lib/hotkeys";
import { navigate } from "@/lib/router";

/** App-wide shortcuts. Pages add their own with useHotkey (later registrations win). */
export function GlobalHotkeys() {
  useHotkey("Space", () => player.toggleSelection());
  // works while typing; pauses while a Modal is open. Seedbox only: Setbox has no search page.
  useHotkey("Mod+k", (e) => (e.shiftKey ? false : focusSearch()), { allowInInputs: true, enabled: !isSetbox });
  // Seedbox: Search, Crates, Downloads. Setbox: Crates, Setlists.
  useHotkey("Mod+1", () => navigate(isSetbox ? { name: "crates" } : { name: "search" }));
  useHotkey("Mod+2", () => navigate(isSetbox ? { name: "setlists" } : { name: "crates" }));
  useHotkey("Mod+3", () => (isSetbox ? false : navigate({ name: "downloads", jobId: "" })));
  useHotkey("Mod+,", () => navigate({ name: "settings" }));
  useHotkey("Mod+[", () => history.back());
  useHotkey("Mod+]", () => history.forward());
  useHotkey("Mod+.", () => player.stop());
  return null;
}
