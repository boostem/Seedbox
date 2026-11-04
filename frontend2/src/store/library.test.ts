import { describe, expect, it } from "vitest";
import { row } from "@/features/library/filter.test";
import { patchRows } from "./library";

describe("patchRows", () => {
  it("replaces rows in place, drops the gone and appends the new", () => {
    const [a, b, c] = [row(), row(), row()];
    const a2 = { ...a, bpm: 128 };
    const d = row();
    const out = patchRows([a, b, c], [a2, d], [a.hash, b.hash, d.hash]);
    expect(out).toEqual([a2, c, d]);
  });
});
