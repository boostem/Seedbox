import { describe, expect, it } from "vitest";
import { energyStars, fmtBpm, fmtDateTime, fmtMb, fmtPitch, fmtRelative, fmtTime, pitchTone, plural, tildify } from "./format";

describe("fmtTime", () => {
  it("formats minutes and hours", () => {
    expect(fmtTime(194.9)).toBe("3:14");
    expect(fmtTime(0)).toBe("0:00");
    expect(fmtTime(3723)).toBe("1:02:03");
  });
  it("renders a placeholder for missing or invalid input", () => {
    for (const v of [null, undefined, Number.NaN, Infinity, -1]) expect(fmtTime(v)).toBe("–:––");
  });
});

describe("fmtBpm", () => {
  it("drops the decimal for (near-)integer tempos", () => {
    expect(fmtBpm(128)).toBe("128");
    expect(fmtBpm(127.97)).toBe("128");
    expect(fmtBpm(127.5)).toBe("127.5");
  });
  it("renders a dash for 0 or invalid", () => {
    for (const v of [0, null, undefined, Number.NaN]) expect(fmtBpm(v)).toBe("—");
  });
});

describe("fmtPitch", () => {
  it("signs the rounded value", () => {
    expect(fmtPitch(1.53)).toBe("+1.5%");
    expect(fmtPitch(-2.26)).toBe("−2.3%");
    expect(fmtPitch(-0.04)).toBe("±0.0%");
    expect(fmtPitch(0)).toBe("±0.0%");
    expect(fmtPitch(1.234, 2)).toBe("+1.23%");
  });
  it("renders a dash for missing input", () => {
    expect(fmtPitch(null)).toBe("—");
    expect(fmtPitch(Number.NaN)).toBe("—");
  });
});

describe("pitchTone", () => {
  it("grades by how far the deck is pushed", () => {
    expect(pitchTone(null)).toBe("text-fg-faint");
    expect(pitchTone(0.04)).toBe("text-fg-subtle");
    expect(pitchTone(-4)).toBe("text-fg-muted");
    expect(pitchTone(4.5)).toBe("text-warn");
    expect(pitchTone(-6.1)).toBe("text-danger");
  });
});

describe("fmtRelative", () => {
  const now = Date.parse("2026-06-15T12:00:00Z");
  const ago = (s: number) => new Date(now - s * 1000).toISOString();
  it("buckets recent times", () => {
    expect(fmtRelative(ago(10), now)).toBe("just now");
    expect(fmtRelative(ago(5 * 60), now)).toBe("5 min ago");
    expect(fmtRelative(ago(3 * 3600), now)).toBe("3 h ago");
    expect(fmtRelative(ago(30 * 3600), now)).toBe("yesterday");
    expect(fmtRelative(ago(3 * 86400), now)).toBe("3 days ago");
  });
  it("shows the year only for other years", () => {
    expect(fmtRelative("2026-01-02T12:00:00Z", now)).not.toMatch(/2026/);
    expect(fmtRelative("2024-09-12T12:00:00Z", now)).toMatch(/2024/);
  });
  it("renders a dash for missing or unparseable dates", () => {
    expect(fmtRelative(null, now)).toBe("—");
    expect(fmtRelative("not a date", now)).toBe("—");
  });
});

describe("fmtDateTime", () => {
  it("renders a dash for missing or unparseable dates", () => {
    expect(fmtDateTime(undefined)).toBe("—");
    expect(fmtDateTime("nope")).toBe("—");
    expect(fmtDateTime("2026-06-15T12:00:00Z")).not.toBe("—");
  });
});

describe("energyStars", () => {
  it("halves the rating, rounding up, clamped to 1-5", () => {
    expect(energyStars(1)).toBe(1);
    expect(energyStars(7)).toBe(4);
    expect(energyStars(10)).toBe(5);
    expect(energyStars(14)).toBe(5);
    expect(energyStars(null)).toBe(0);
    expect(energyStars(0)).toBe(0);
  });
});

describe("small helpers", () => {
  it("plural", () => {
    expect(plural(1, "track")).toBe("1 track");
    expect(plural(0, "track")).toBe("0 tracks");
    expect(plural(2, "mix", "mixes")).toBe("2 mixes");
  });
  it("tildify", () => {
    expect(tildify("/Users/x/Music/A - B")).toBe("~/Music/A - B");
    expect(tildify("/home/x/Music")).toBe("/home/x/Music");
    expect(tildify(null)).toBe("");
  });
  it("fmtMb", () => {
    expect(fmtMb(500)).toBe("500 MB");
    expect(fmtMb(1500)).toBe("1.5 GB");
    expect(fmtMb(2000)).toBe("2 GB");
  });
});
