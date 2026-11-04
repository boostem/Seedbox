/** What is wrong with this install or connection, and how to fix it: the banner under the top bar. */
import type { SystemInfo } from "@/api/types";
import type { ConnectionState } from "@/store/app";

export interface SystemProblem {
  id: "offline" | "ffmpeg" | "deno";
  tone: "danger" | "warn";
  title: string;
  fix: string;
  /** Fixed outside the app: offer to look again. */
  recheck?: boolean;
}

const INSTALL: Record<string, Record<"ffmpeg" | "deno", string>> = {
  darwin: { ffmpeg: "brew install ffmpeg", deno: "brew install deno" },
  win32: { ffmpeg: "winget install ffmpeg", deno: "winget install DenoLand.Deno" },
};

function howToGet(tool: "ffmpeg" | "deno", system: SystemInfo): string {
  if (system.frozen) return `Reinstall Seedbox 2: it comes with ${tool}.`;
  const command = INSTALL[system.platform]?.[tool];
  if (command) return `Install it with ${command}.`;
  return `Install ${tool} with your package manager (${tool === "ffmpeg" ? "sudo apt install ffmpeg" : "see deno.com"}).`;
}

export function systemProblems(system: SystemInfo | null, connection: ConnectionState): SystemProblem[] {
  if (connection === "offline") {
    return [{ id: "offline", tone: "danger", title: "The Seedbox 2 engine isn't responding", fix: "It reconnects by itself. If it doesn't, quit Seedbox 2 and open it again." }];
  }
  if (!system) return [];
  const problems: SystemProblem[] = [];
  if (!system.ffmpeg) {
    problems.push({ id: "ffmpeg", tone: "danger", title: "ffmpeg is missing, so Seedbox 2 can't read audio", fix: howToGet("ffmpeg", system), recheck: true });
  }
  if (!system.deno) {
    problems.push({ id: "deno", tone: "warn", title: "deno is missing, so downloads from YouTube won't work", fix: `Your own music is fine without it. ${howToGet("deno", system)}`, recheck: true });
  }
  return problems;
}
