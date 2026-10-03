/** yt-dlp runtime update (top bar button + Settings page share this state). */
import { create } from "zustand";
import { api, type YtdlpUpdateResult } from "@/api/client";
import { useApp } from "./app";
import { toast } from "./toasts";

interface UpdateState {
  running: boolean;
  startedAt: number | null;
  result: YtdlpUpdateResult | null;
  error: string | null;
}

export const useYtdlpUpdate = create<UpdateState>()(() => ({ running: false, startedAt: null, result: null, error: null }));

export async function runYtdlpUpdate(): Promise<void> {
  if (useYtdlpUpdate.getState().running) return;
  useYtdlpUpdate.setState({ running: true, startedAt: Date.now(), result: null, error: null });
  try {
    const result = await api.updateYtdlp({ quiet: true });
    useYtdlpUpdate.setState({ running: false, result });
    if (result.ok) toast.success(result.message || `yt-dlp ${result.version} installed`, { title: "yt-dlp updated: applies on next launch", timeout: 8000 });
    else toast.warning(result.message, { title: "yt-dlp update" });
  } catch (e) {
    const msg = (e as Error).message;
    useYtdlpUpdate.setState({ running: false, error: msg });
    toast.error(msg, { title: "yt-dlp update failed" });
  }
  try {
    useApp.getState().setSystem(await api.system({ quiet: true }));
  } catch {
    /* keep old status */
  }
}

/** True when the backend offers an update (it only sets ytdlp_latest when one is worth installing). */
export function ytdlpOutdated(installed: string | undefined, latest: string | null | undefined): boolean {
  return !!latest && latest !== installed;
}

/** Version installed by an update this session that takes effect after a restart (null if none). */
export function useYtdlpPendingRestart(running: string | undefined): string | null {
  return useYtdlpUpdate((s) => (s.result?.ok && s.result.version && s.result.version !== running ? s.result.version : null));
}
