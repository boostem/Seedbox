import { describe, expect, it } from "vitest";
import type { AppUpdateStatus } from "@/api/types";
import { appUpdateLine } from "./updates";

const base: AppUpdateStatus = {
  current: "2.0.0",
  enabled: true,
  installable: true,
  reason: null,
  state: "idle",
  latest: null,
  progress: null,
  error: null,
  checked_at: null,
  install_on_exit: false,
  releases_url: "https://github.com/o/r/releases",
};
const release = { version: "2.1.0", tag: "seedbox2-v2.1.0", name: "2.1.0", url: "u", notes: "", published_at: null, asset: { name: "a.dmg", size: 100, url: "x" } };

describe("appUpdateLine", () => {
  it("offers the restart only for a ready update that can install here", () => {
    expect(appUpdateLine({ ...base, state: "ready", latest: release })).toMatchObject({ tone: "ok", canInstall: true });
    expect(appUpdateLine({ ...base, state: "ready", latest: release, installable: false }).canInstall).toBe(false);
  });

  it("shows download progress", () => {
    const line = appUpdateLine({ ...base, state: "downloading", latest: release, progress: { done: 25_000_000, total: 100_000_000 } });
    expect(line.progress).toBeCloseTo(0.25);
    expect(line.text).toContain("25 MB of 100 MB");
  });

  it("says when the last install failed but keeps the update ready", () => {
    const line = appUpdateLine({ ...base, state: "ready", latest: release, error: "The update to 2.1.0 didn't install (disk full)." });
    expect(line).toMatchObject({ tone: "warn", canInstall: true });
  });

  it("links to the release when it can't install it (from source)", () => {
    expect(appUpdateLine({ ...base, installable: false, state: "available", latest: release }).text).toContain("release page");
  });

  it("reports errors and the newest version", () => {
    expect(appUpdateLine({ ...base, state: "error", error: "offline" }).tone).toBe("danger");
    expect(appUpdateLine({ ...base, state: "up_to_date" }).text).toBe("Seedbox 2 2.0.0 is the newest version.");
  });
});
