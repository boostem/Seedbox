/**
 * The full set mix of a crate ("crate:<folder>") or a setlist ("setlist:<id>"): GET …/setmix for the last mix +
 * whether the set changed since, live updates from `setmix` events (store), start / cancel.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { SetMix } from "@/api/types";
import { useApp, useSetMixLive } from "@/store/app";

export interface SetMixApi {
  mix: SetMix | null;
  stale: boolean;
  loading: boolean;
  starting: boolean;
  /** queued / preparing / rendering */
  busy: boolean;
  start: () => Promise<void>;
  cancel: () => Promise<void>;
}

export const isBusy = (m: SetMix | null | undefined) => !!m && (m.state === "queued" || m.state === "preparing" || m.state === "rendering");

export function useSetMix(target: string, refreshKey: unknown = 0): SetMixApi {
  const live = useSetMixLive(target);
  const epoch = useApp((s) => s.epoch);
  const [fetched, setFetched] = useState<{ mix: SetMix | null; stale: boolean } | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const liveDone = live?.state === "done" ? live.mix_id : null;

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    api
      .setMixStatus(target, { quiet: true, signal: ctrl.signal })
      .then((r) => {
        setFetched(r);
        if (r.mix) useApp.getState().upsertSetMix(r.mix);
      })
      .catch(() => {})
      .finally(() => !ctrl.signal.aborted && setLoading(false));
    return () => ctrl.abort();
  }, [target, epoch, liveDone, refreshKey]);

  // the newer of the fetched status and the live event stream
  let mix = fetched?.mix ?? null;
  if (live && (!mix || live.mix_id === mix.mix_id || live.started_at >= mix.started_at)) mix = live;
  const stale = !!fetched?.stale && fetched.mix?.mix_id === mix?.mix_id;

  const start = useCallback(async () => {
    setStarting(true);
    try {
      const m = await api.startSetMix(target);
      useApp.getState().upsertSetMix(m);
    } catch {
      /* toasted by the client */
    } finally {
      setStarting(false);
    }
  }, [target]);

  const cancel = useCallback(async () => {
    if (!mix) return;
    try {
      useApp.getState().upsertSetMix(await api.cancelSetMix(mix.mix_id));
    } catch {
      /* toasted */
    }
  }, [mix]);

  return { mix, stale, loading, starting, busy: isBusy(mix), start, cancel };
}
