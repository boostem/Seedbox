import { player } from "@/audio/player";
import { useHotkey } from "@/lib/hotkeys";
import { navigate } from "@/lib/router";

/** App-wide shortcuts. Pages add their own with useHotkey (later registrations win). */
export function GlobalHotkeys() {
  useHotkey("Space", () => player.toggle());
  useHotkey("Mod+1", () => navigate({ name: "library" }));
  useHotkey("Mod+2", () => navigate({ name: "sets" }));
  useHotkey("Mod+3", () => navigate({ name: "mixes" }));
  useHotkey("Mod+,", () => navigate({ name: "system" }));
  useHotkey("Mod+[", () => history.back());
  useHotkey("Mod+]", () => history.forward());
  useHotkey("Mod+.", () => player.stop());
  return null;
}
