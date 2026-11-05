/** What low-power mode is doing and why, in words (#85), for the top bar and Settings. */
import type { PowerStatus } from "@/api/types";

/** Why it's on: "On battery, 64% left". */
export function powerWhy(s: PowerStatus): string {
  if (s.reason === "battery") return s.battery_percent === null ? "On battery" : `On battery, ${s.battery_percent}% left`;
  if (s.reason === "thermal") return s.thermal === "critical" ? "This computer is very hot" : "This computer is running hot";
  if (s.reason === "manual") return "You turned it on";
  return "";
}

/** The top bar's few words: "On battery: stems paused". */
export function powerLabel(s: PowerStatus): string {
  const who = s.reason === "battery" ? "On battery" : s.reason === "thermal" ? "Running hot" : "Low power";
  const what = s.limits.analysis_paused ? "analysis paused" : s.limits.stems_paused ? "stems paused" : "slower analysis";
  return `${who}: ${what}`;
}

/** When the waiting ends, for "…wait until". */
function until(s: PowerStatus): string {
  if (s.reason === "battery") return "you plug in";
  if (s.reason === "thermal") return "it cools down";
  return "you turn low-power mode off";
}

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** One sentence per thing held back. `waitingStems`, `waitingAnalysis`: how many of those jobs are queued. */
export function powerEffects(s: PowerStatus, waitingStems: number, waitingAnalysis: number): string[] {
  const l = s.limits;
  const out: string[] = [];
  if (l.stems_paused) {
    const waiting = waitingStems ? ` (${count(waitingStems, "track")} waiting)` : "";
    out.push(`Stems of the library are paused until ${until(s)}${waiting}. A set you open still gets its stems, from the smaller INT8 model.`);
  }
  if (l.analysis_paused) {
    const waiting = waitingAnalysis ? ` (${count(waitingAnalysis, "job")} waiting)` : "";
    out.push(`Analysis of the library is paused until ${until(s)}${waiting}. What you open still runs.`);
  }
  if (l.light_workers < l.light_workers_full) out.push(`Analysis runs on ${l.light_workers} of ${count(l.light_workers_full, "worker")}.`);
  out.push("New beat analysis uses the small model (Beat This! small0).");
  if (l.fps) out.push(`Waveforms draw at ${l.fps} fps.`);
  return out;
}

/** How the machine is doing, for Settings: "On battery, 64% left. Temperature normal." */
export function powerSource(s: PowerStatus): string {
  const source = s.on_battery === null ? "No battery" : s.on_battery ? (s.battery_percent === null ? "On battery" : `On battery, ${s.battery_percent}% left`) : "Plugged in";
  const heat = { nominal: "temperature normal", fair: "temperature normal", serious: "running hot", critical: "very hot", unknown: "" }[s.thermal];
  return heat ? `${source}, ${heat}` : source;
}
