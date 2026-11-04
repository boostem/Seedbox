import { Boxes, Compass, Library, ListMusic, type LucideIcon, Settings } from "lucide-react";
import type { RouteName } from "@/lib/router";

export interface NavEntry {
  route: Exclude<RouteName, "notFound">;
  label: string;
  icon: LucideIcon;
  combo: string;
}

/** The icon rail, top to bottom (Settings sits at the bottom). Home is the logo; Mixes and the rest are in ⌘K. */
export const RAIL: NavEntry[] = [
  { route: "library", label: "Library", icon: Library, combo: "Mod+1" },
  { route: "crates", label: "Crates", icon: Boxes, combo: "Mod+2" },
  { route: "sets", label: "Sets", icon: ListMusic, combo: "Mod+3" },
  { route: "discover", label: "Discover", icon: Compass, combo: "Mod+4" },
];
export const SETTINGS_NAV: NavEntry = { route: "settings", label: "Settings", icon: Settings, combo: "Mod+," };

export const TITLES: Record<RouteName, string> = {
  home: "Home",
  library: "Library",
  crates: "Crates",
  sets: "Sets",
  discover: "Discover",
  mixes: "Mixes",
  settings: "Settings",
  ui: "Design system",
  bench: "Benchmark",
  listen: "Listening test",
  notFound: "Not found",
};
