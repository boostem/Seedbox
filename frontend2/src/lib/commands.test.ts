import { beforeEach, describe, expect, it } from "vitest";
import { type Command, commandsByGroup, paletteResults, registerCommands, registerPaletteSource, useCommands } from "./commands";

const noop = () => {};
const cmd = (id: string, title: string, group: Command["group"], extra: Partial<Command> = {}): Command => ({ id, title, group, run: noop, ...extra });

const COMMANDS = [
  cmd("app.palette", "Command palette", "App", { combo: "Mod+K", paletteHidden: true }),
  cmd("go.library", "Go to Library", "Go to", { combo: "Mod+1" }),
  cmd("set.new", "New set", "Actions", { keywords: "create plan" }),
  cmd("play.toggle", "Play or pause", "Playback", { combo: "Space" }),
  cmd("go.sets", "Go to Sets", "Go to", { combo: "Mod+3" }),
  cmd("mix.export", "Export mix", "Actions", { enabled: () => false }),
];

describe("paletteResults", () => {
  it("lists every offered command by group when the query is empty", () => {
    expect(paletteResults("", COMMANDS, []).map((r) => r.key)).toEqual(["c:go.library", "c:go.sets", "c:set.new", "c:play.toggle"]);
  });

  it("ranks matches across groups and sources, best first", () => {
    const tracks = { id: "tracks", group: "Tracks", search: () => [{ id: "t1", title: "Settle Down", run: noop }, { id: "t2", title: "Strobe", run: noop }] };
    const keys = paletteResults("set", COMMANDS, [tracks]).map((r) => r.key);
    expect(keys).toContain("c:go.sets");
    expect(keys).toContain("c:set.new");
    expect(keys).toContain("tracks:t1");
    expect(keys).not.toContain("tracks:t2");
    expect(keys).not.toContain("c:mix.export"); // disabled
  });

  it("matches keywords and group names", () => {
    expect(paletteResults("create", COMMANDS, []).map((r) => r.key)).toEqual(["c:set.new"]);
    expect(paletteResults("playback", COMMANDS, []).map((r) => r.key)).toEqual(["c:play.toggle"]);
  });

  it("survives a source that throws", () => {
    const broken = { id: "x", group: "X", search: () => { throw new Error("boom"); } };
    expect(paletteResults("lib", COMMANDS, [broken]).map((r) => r.key)).toEqual(["c:go.library"]);
  });
});

describe("registry", () => {
  beforeEach(() => useCommands.setState({ commands: [], sources: [] }));

  it("registers and unregisters, a later id replacing an earlier one", () => {
    const off1 = registerCommands([cmd("a", "A", "App")]);
    const off2 = registerCommands([cmd("a", "A again", "App"), cmd("b", "B", "App")]);
    expect(useCommands.getState().commands.map((c) => c.title)).toEqual(["A again", "B"]);
    off1(); // already replaced: removes nothing
    expect(useCommands.getState().commands).toHaveLength(2);
    off2();
    expect(useCommands.getState().commands).toEqual([]);
    const offSrc = registerPaletteSource({ id: "s", group: "Sets", search: () => [] });
    expect(useCommands.getState().sources).toHaveLength(1);
    offSrc();
    expect(useCommands.getState().sources).toHaveLength(0);
  });

  it("groups every command for the shortcuts sheet, palette-hidden ones too", () => {
    expect(commandsByGroup(COMMANDS).map((g) => [g.group, g.commands.length])).toEqual([["Go to", 2], ["Actions", 2], ["Playback", 1], ["App", 1]]);
  });
});
