/**
 * Every action in the app, in one registry: the command palette (⌘K) runs them, GlobalHotkeys binds their combos and
 * the shortcuts sheet (?) lists them, so an action added here is reachable from the keyboard and documented.
 *
 *   useRegisterCommands([{ id: "set.save", title: "Save set", group: "Actions", combo: "Mod+S", run: save }], [save])
 *   registerPaletteSource({ id: "tracks", group: "Tracks", search: (q) => tracks.filter(…).map(toItem) })
 *
 * Pages register their own commands while mounted; the shell's are in components/shell/appCommands.ts. Areas that
 * hold things to go to (tracks, crates, sets) register a palette source.
 */
import type { LucideIcon } from "lucide-react";
import { type DependencyList, useEffect } from "react";
import { create } from "zustand";
import { type FuzzyMatch, fuzzyRank } from "./fuzzy";

export type CommandGroup = "Go to" | "Actions" | "Playback" | "View" | "App";
/** Palette and shortcuts-sheet order. */
export const COMMAND_GROUPS: CommandGroup[] = ["Go to", "Actions", "Playback", "View", "App"];

export interface Command {
  id: string;
  title: string;
  group: CommandGroup;
  icon?: LucideIcon;
  /** Keyboard shortcut ("Mod+K", "Shift+?", "Space"); see lib/hotkeys.ts. */
  combo?: string;
  /** More combos for the same command, bound but not shown ("Shift+Equal" for "="). */
  altCombos?: string[];
  /** Extra words the palette matches ("render audio" for Export). */
  keywords?: string;
  /** Short note next to the title in the palette ("Soon"). */
  hint?: string;
  /** The shortcut also fires while typing in a field / while a dialog is open. */
  allowInInputs?: boolean;
  allowInModal?: boolean;
  /** Not offered in the palette (the palette's own command), still listed in the shortcuts sheet. */
  paletteHidden?: boolean;
  enabled?: () => boolean;
  run: () => void;
}

/** Something to go to: a track, a crate, a set. */
export interface PaletteItem {
  id: string;
  title: string;
  subtitle?: string;
  icon?: LucideIcon;
  run: () => void;
}

export interface PaletteSource {
  id: string;
  /** The palette's label for its items ("Tracks", "Crates", "Sets"). */
  group: string;
  /** Candidates for a non-empty query (the palette ranks them); keep it fast and capped, it runs on every keystroke. */
  search: (query: string) => PaletteItem[];
}

interface CommandsState {
  commands: Command[];
  sources: PaletteSource[];
}

export const useCommands = create<CommandsState>()(() => ({ commands: [], sources: [] }));

/** Add commands; returns a function that removes them. A later command with the same id replaces an earlier one. */
export function registerCommands(cmds: Command[]): () => void {
  useCommands.setState((s) => ({ commands: [...s.commands.filter((c) => !cmds.some((n) => n.id === c.id)), ...cmds] }));
  return () => useCommands.setState((s) => ({ commands: s.commands.filter((c) => !cmds.includes(c)) }));
}

export function registerPaletteSource(source: PaletteSource): () => void {
  useCommands.setState((s) => ({ sources: [...s.sources.filter((x) => x.id !== source.id), source] }));
  return () => useCommands.setState((s) => ({ sources: s.sources.filter((x) => x !== source) }));
}

/** Register commands while the component is mounted (re-registered when `deps` change). */
export function useRegisterCommands(cmds: Command[], deps: DependencyList): void {
  useEffect(() => registerCommands(cmds), deps);
}

export function isEnabled(c: Command): boolean {
  return c.enabled ? c.enabled() : true;
}

export type PaletteResult =
  | { kind: "command"; key: string; group: string; command: Command; match: FuzzyMatch }
  | { kind: "item"; key: string; group: string; item: PaletteItem; match: FuzzyMatch };

/**
 * What the palette shows for `query`: with no query, every command by group; otherwise commands and source items
 * ranked together by fuzzy score (best first), at most `limit`.
 */
export function paletteResults(query: string, commands: Command[], sources: PaletteSource[], limit = 50): PaletteResult[] {
  const offered = commands.filter((c) => !c.paletteHidden && isEnabled(c));
  if (!query.trim()) {
    return COMMAND_GROUPS.flatMap((g) => offered.filter((c) => c.group === g)).map((command) => ({
      kind: "command" as const,
      key: `c:${command.id}`,
      group: command.group,
      command,
      match: { score: 0, indices: [] },
    }));
  }
  const ranked: PaletteResult[] = fuzzyRank(query, offered, (c) => c.title, (c) => `${c.group} ${c.keywords ?? ""}`).map(({ item, match }) => ({
    kind: "command" as const,
    key: `c:${item.id}`,
    group: item.group,
    command: item,
    match,
  }));
  for (const src of sources) {
    let found: PaletteItem[] = [];
    try {
      found = src.search(query);
    } catch {
      continue; // a broken source mustn't break the palette
    }
    for (const { item, match } of fuzzyRank(query, found, (i) => i.title, (i) => i.subtitle)) {
      ranked.push({ kind: "item", key: `${src.id}:${item.id}`, group: src.group, item, match });
    }
  }
  return ranked.sort((a, b) => b.match.score - a.match.score).slice(0, limit);
}

/** Commands for the shortcuts sheet, by group (every command, palette-hidden ones too). */
export function commandsByGroup(commands: Command[]): { group: CommandGroup; commands: Command[] }[] {
  return COMMAND_GROUPS.map((group) => ({ group, commands: commands.filter((c) => c.group === group) })).filter((g) => g.commands.length);
}
