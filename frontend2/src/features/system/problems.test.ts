import { describe, expect, it } from "vitest";
import type { SystemInfo } from "@/api/types";
import { systemProblems } from "./problems";

const system = (over: Partial<SystemInfo>) => ({ platform: "linux", frozen: false, ffmpeg: "/usr/bin/ffmpeg", deno: "/usr/bin/deno", ...over }) as SystemInfo;

describe("systemProblems", () => {
  it("finds nothing wrong on a working install, or before it knows", () => {
    expect(systemProblems(system({}), "live")).toEqual([]);
    expect(systemProblems(null, "connecting")).toEqual([]);
  });

  it("says to reinstall the packaged app when ffmpeg is missing", () => {
    const [p] = systemProblems(system({ ffmpeg: null, frozen: true }), "live");
    expect(p.id).toBe("ffmpeg");
    expect(p.tone).toBe("danger");
    expect(p.fix).toContain("Reinstall");
    expect(p.recheck).toBe(true);
  });

  it("gives the install command when run from source", () => {
    expect(systemProblems(system({ ffmpeg: null, platform: "darwin" }), "live")[0].fix).toContain("brew install ffmpeg");
    expect(systemProblems(system({ ffmpeg: null, platform: "win32" }), "live")[0].fix).toContain("winget install ffmpeg");
    expect(systemProblems(system({ ffmpeg: null }), "live")[0].fix).toContain("package manager");
  });

  it("treats deno as a warning: local music works without it", () => {
    const [p] = systemProblems(system({ deno: null }), "live");
    expect(p.tone).toBe("warn");
    expect(p.fix).toContain("Your own music is fine");
  });

  it("reports only the lost engine when offline", () => {
    const problems = systemProblems(system({ ffmpeg: null }), "offline");
    expect(problems.map((p) => p.id)).toEqual(["offline"]);
  });
});
