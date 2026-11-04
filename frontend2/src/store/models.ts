/**
 * Models (#42): the latest GET /api/models status, kept current by the `model.progress` and `models` events, so the
 * Settings → Models panel and anything that says "running without beat_this" read one store. Finished downloads
 * toast.
 */
import { create } from "zustand";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { ModelsStatus } from "@/api/types";
import { toast } from "@/store/toasts";

interface ModelsState {
  status: ModelsStatus | null;
  set: (status: ModelsStatus) => void;
  /** Bytes downloaded so far, from a `model.progress` event. */
  progress: (name: string, done: number) => void;
  refresh: () => Promise<void>;
}

export const useModels = create<ModelsState>()((set) => ({
  status: null,
  set: (status) => set({ status }),
  progress: (name, done) =>
    set((s) =>
      s.status
        ? { status: { ...s.status, models: s.status.models.map((m) => (m.name === name ? { ...m, state: "downloading", done } : m)) } }
        : s,
    ),
  refresh: async () => {
    try {
      set({ status: await api.models.status({ quiet: true }) });
    } catch {
      // the engine is down: the connection dot says so
    }
  },
}));

/** What to do about a failed download, from its error. It resumes where it stopped and the app works meanwhile. */
export function modelFix(error: string | null | undefined): string {
  const e = (error ?? "").toLowerCase();
  if (/no space|disk full|enospc/.test(e)) return "The disk is full: free some space, then retry.";
  if (/sha|checksum|hash/.test(e)) return "The file arrived damaged: retry to fetch it again.";
  return "Check the connection, then retry: it carries on where it stopped.";
}

export function retryModel(name: string): void {
  void api.models.download(name).then(() => useModels.getState().refresh(), () => {});
}

function titleOf(name: string): string {
  return useModels.getState().status?.models.find((m) => m.name === name)?.title ?? name;
}

let started = false;
/** Follow the `model.*` and `models` events (once, from <App>). */
export function followModels(): () => void {
  if (started) return () => {};
  started = true;
  void useModels.getState().refresh();
  const off = onServerEvent((ev) => {
    const data = ev.data as { name?: string; done?: number; error?: string };
    if (ev.type === "models") useModels.getState().set(ev.data as ModelsStatus);
    else if (ev.type === "model.progress" && data.name) useModels.getState().progress(data.name, data.done ?? 0);
    else if (ev.type === "model.done" && data.name) toast.success(`${titleOf(data.name)} is ready`, { key: `model:${data.name}`, title: "Models" });
    else if (ev.type === "model.failed" && data.name) {
      const name = data.name;
      toast.error(`${titleOf(name)} didn't download. ${modelFix(data.error)}`, { key: `model:${name}`, title: "Models", action: { label: "Retry", onClick: () => retryModel(name) } });
    }
  });
  return () => {
    started = false;
    off();
  };
}
