// Shared job/item status helpers for the Downloads screens (owner: FRONTEND-CRATE).
import type { BadgeTone } from "@/components/ui";
import type { FinalStatus, Job, JobItem } from "@/api/types";

export const JOB_STATE_LABEL: Record<Job["state"], string> = {
  running: "Downloading",
  finalizing: "Finalizing crate",
  done: "Done",
  cancelled: "Cancelled",
};

export const JOB_STATE_TONE: Record<Job["state"], BadgeTone> = {
  running: "accent",
  finalizing: "info",
  done: "ok",
  cancelled: "warn",
};

export const STATUS_TONE: Record<FinalStatus, BadgeTone> = {
  downloaded: "ok",
  copied: "info",
  exists: "neutral",
  skipped: "warn",
  failed: "danger",
};

export const isLive = (job: Job): boolean => job.state === "running" || job.state === "finalizing";

/** Finished items / all items, and per-status counts. */
export function jobProgress(job: Job): { done: number; total: number; counts: Partial<Record<FinalStatus, number>> } {
  const counts: Partial<Record<FinalStatus, number>> = {};
  let done = 0;
  for (const it of job.items) {
    if (!it.status) continue;
    done++;
    counts[it.status] = (counts[it.status] ?? 0) + 1;
  }
  return { done, total: job.items.length, counts };
}

/** "3 downloaded · 1 failed" (empty when nothing finished yet). */
export function countsSummary(counts: Partial<Record<FinalStatus, number>>): string {
  return Object.entries(counts)
    .map(([k, n]) => `${n} ${k}`)
    .join(" · ");
}

/** An item that has left the queue but not finished. */
export const isActiveItem = (it: JobItem): boolean => !it.status && it.text.trim().toLowerCase() !== "queued";
