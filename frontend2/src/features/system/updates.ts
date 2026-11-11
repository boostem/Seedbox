/** What Settings → Updates says about Seedbox 2's own update (#89). Pure, so it is tested without a server. */
import type { AppUpdateStatus } from "@/api/types";
import { fmtBytes } from "@/lib/format";

export type UpdateTone = "ok" | "warn" | "danger" | "info";

export interface UpdateLine {
  tone: UpdateTone;
  text: string;
  /** "Restart to update" is offered. */
  canInstall: boolean;
  /** 0–1 while downloading, else null. */
  progress: number | null;
}

export function appUpdateLine(s: AppUpdateStatus): UpdateLine {
  const v = s.latest?.version;
  const base = { canInstall: false, progress: null };
  switch (s.state) {
    case "checking":
      return { ...base, tone: "info", text: "Checking for a new version…" };
    case "downloading": {
      const p = s.progress;
      const of = p && p.total > 0 ? ` (${fmtBytes(p.done)} of ${fmtBytes(p.total)})` : "";
      return { ...base, tone: "info", text: `Downloading Seedbox 2 ${v ?? ""}${of}`.replace("  ", " "), progress: p && p.total > 0 ? Math.min(1, p.done / p.total) : null };
    }
    case "ready":
      return {
        ...base,
        tone: s.error ? "warn" : "ok",
        text: s.error ? `${s.error} It's still ready to try again.` : `Seedbox 2 ${v} is downloaded and verified. It installs when you restart.`,
        canInstall: s.installable,
      };
    case "available":
      return { ...base, tone: "info", text: `Seedbox 2 ${v} is out.${s.installable ? "" : " Get it from its release page."}` };
    case "up_to_date":
      return { ...base, tone: "ok", text: `Seedbox 2 ${s.current} is the newest version.` };
    case "error":
      return { ...base, tone: "danger", text: `Couldn't check for updates: ${s.error ?? "unknown error"}` };
    default:
      return { ...base, tone: "info", text: s.enabled && s.installable ? "Checks shortly after startup." : "Not checked yet." };
  }
}
