import { describe, expect, it } from "vitest";
import { CAMELOT_CODES, CAMELOT_COLORS, camelotAdjacent, camelotMove, formatKey, keyColor, parseCamelot, toOpenKey } from "./camelot";

describe("parseCamelot", () => {
  it("normalises valid codes", () => {
    expect(parseCamelot("1A")).toEqual({ n: 1, letter: "A", code: "1A" });
    expect(parseCamelot(" 08b ")).toEqual({ n: 8, letter: "B", code: "8B" });
    expect(parseCamelot("12a")?.code).toBe("12A");
  });
  it("rejects out-of-range or malformed codes", () => {
    for (const c of ["0A", "13B", "1C", "A1", "", null, undefined]) expect(parseCamelot(c)).toBeNull();
  });
});

describe("colours", () => {
  it("has a colour for all 24 codes", () => {
    expect(CAMELOT_CODES).toHaveLength(24);
    for (const c of CAMELOT_CODES) expect(CAMELOT_COLORS[c].base).toMatch(/^hsl\(/);
  });
  it("looks codes up leniently and falls back to grey", () => {
    expect(keyColor("05a")).toBe(CAMELOT_COLORS["5A"]);
    expect(keyColor("5A")).not.toEqual(keyColor("5B"));
    expect(keyColor("??").base).toBe("var(--color-key-unknown)");
  });
});

describe("camelotMove", () => {
  it.each([
    ["8A", "8A", "same"],
    ["8A", "9A", "+1"],
    ["8A", "7A", "-1"],
    ["12A", "1A", "+1"],
    ["1B", "12B", "-1"],
    ["8A", "10A", "+2 boost"],
    ["8A", "3A", "+7 boost"],
    ["8A", "8B", "relative"],
    ["8A", "9B", "diagonal"],
    ["8B", "7A", "diagonal"],
    ["8A", "11A", "clash"],
    ["8A", "11B", "clash"],
    ["8A", null, "?"],
    ["x", "8A", "?"],
  ] as const)("%s → %s is %s", (a, b, move) => {
    expect(camelotMove(a, b)).toBe(move);
  });
});

describe("camelotAdjacent", () => {
  it("accepts same, ±1 and relative only", () => {
    expect(camelotAdjacent("8A", "8A")).toBe(true);
    expect(camelotAdjacent("8A", "9A")).toBe(true);
    expect(camelotAdjacent("8A", "8B")).toBe(true);
    expect(camelotAdjacent("8A", "9B")).toBe(false);
    expect(camelotAdjacent("8A", "10A")).toBe(false);
  });
});

describe("key notation", () => {
  it("maps Camelot to Open Key", () => {
    expect(toOpenKey("8A")).toBe("1m");
    expect(toOpenKey("8B")).toBe("1d");
    expect(toOpenKey("12a")).toBe("5m");
    expect(toOpenKey("1B")).toBe("6d");
    expect(toOpenKey("13A")).toBeNull();
    expect(toOpenKey(null)).toBeNull();
  });

  it("writes a key the way the user wants", () => {
    expect(formatKey("08a", "camelot")).toBe("8A");
    expect(formatKey("8A", "open")).toBe("1m");
    expect(formatKey(null, "open")).toBe("");
  });
});
