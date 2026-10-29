import { Disc3, House, Library, ListMusic, type LucideIcon, Settings } from "lucide-react";
import type { RouteName } from "@/lib/router";

export interface NavEntry {
  route: Exclude<RouteName, "notFound">;
  label: string;
  icon: LucideIcon;
  combo?: string;
}

/** The areas of the app, in sidebar order (System sits at the bottom). */
export const NAV: NavEntry[] = [
  { route: "home", label: "Home", icon: House },
  { route: "library", label: "Library", icon: Library, combo: "Mod+1" },
  { route: "sets", label: "Sets", icon: ListMusic, combo: "Mod+2" },
  { route: "mixes", label: "Mixes", icon: Disc3, combo: "Mod+3" },
];
export const SYSTEM_NAV: NavEntry = { route: "system", label: "System", icon: Settings, combo: "Mod+," };

export const TITLES: Record<RouteName, string> = {
  home: "Home",
  library: "Library",
  sets: "Sets",
  mixes: "Mixes",
  system: "System",
  ui: "Design system",
  notFound: "Not found",
};
