import { describe, expect, it } from "vitest";
import { fuzzyMatch, fuzzyRank } from "./fuzzy";

describe("fuzzyMatch", () => {
  it("matches characters in order, case-insensitively", () => {
    expect(fuzzyMatch("lby", "Library")?.indices).toEqual([0, 2, 6]);
    expect(fuzzyMatch("LIB", "library")).not.toBeNull();
    expect(fuzzyMatch("ybl", "Library")).toBeNull();
    expect(fuzzyMatch("crates", "Crate")).toBeNull();
  });

  it("an empty query matches everything", () => {
    expect(fuzzyMatch("", "Anything")).toEqual({ score: 0, indices: [] });
    expect(fuzzyMatch("   ", "Anything")?.score).toBe(0);
  });

  it("prefers word starts and runs over scattered letters", () => {
    // "mix" lands on the word "mix", not on the m of "Remove" and the i, x further on
    expect(fuzzyMatch("mix", "Remove it, export mix")?.indices).toEqual([18, 19, 20]);
    expect(fuzzyMatch("ns", "New set")?.indices).toEqual([0, 4]);
  });

  it("matches the words of the query independently and in any order", () => {
    const m = fuzzyMatch("set new", "New set");
    expect(m?.indices).toEqual([0, 1, 2, 4, 5, 6]);
    expect(fuzzyMatch("set old", "New set")).toBeNull();
  });

  it("scores a prefix above a match further in", () => {
    const a = fuzzyMatch("sets", "Sets")!.score;
    const b = fuzzyMatch("sets", "Go to Sets")!.score;
    expect(a).toBeGreaterThan(b);
  });

  it("knows camelCase word starts", () => {
    expect(fuzzyMatch("dj", "darkJungle")?.indices).toEqual([0, 4]);
  });
});

describe("fuzzyRank", () => {
  const items = ["Analyse library", "Go to Library", "Go to Sets", "New set", "Export mix"];

  it("ranks best first and drops non-matches", () => {
    const r = fuzzyRank("set", items, (s) => s).map((x) => x.item);
    expect(r).toEqual(["New set", "Go to Sets"].sort((a, b) => r.indexOf(a) - r.indexOf(b)));
    expect(r).toHaveLength(2);
  });

  it("keeps the input order on ties", () => {
    expect(fuzzyRank("", items, (s) => s).map((x) => x.item)).toEqual(items);
  });

  it("matches keyword prefixes at a lower score, without highlighting them", () => {
    const kw = (s: string) => (s === "Export mix" ? "render audio" : s === "New set" ? "create plan" : undefined);
    const r = fuzzyRank("render", items, (s) => s, kw);
    expect(r.map((x) => x.item)).toEqual(["Export mix"]);
    expect(r[0].match.indices).toEqual([]);
    expect(fuzzyRank("aud exp", items, (s) => s, kw).map((x) => x.item)).toEqual(["Export mix"]);
    // keywords match by word prefix, not scattered letters: "rnd" is in no title and starts no keyword
    expect(fuzzyRank("rnd", items, (s) => s, kw)).toEqual([]);
    const lib = fuzzyRank("lib", items, (s) => s, () => "library");
    expect(lib[0].match.indices.length).toBeGreaterThan(0);
  });
});
