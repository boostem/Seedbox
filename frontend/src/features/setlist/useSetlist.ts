/**
 * One setlist: GET /api/setlists/{id}, refetched when it changes (`setlist_changed`) or when any of its crates'
 * set order / analyses change; optimistic crate re-ordering, rename, add / remove, auto-order.
 */
import { useCallback, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { Setlist } from "@/api/types";
import { useAsync } from "@/lib/useAsync";
import { useApp, useDebounced, useSetlistVersion } from "@/store/app";
import { toast } from "@/store/toasts";

export function useSetlist(id: string) {
  const version = useSetlistVersion(id);
  const [folders, setFolders] = useState<string[]>([]);
  // member crates' set changes + finished analyses (debounced like useSetRefreshKey)
  const memberKey = useApp((s) =>
    folders.reduce((n, f) => n + (s.setVersion[f] ?? 0) * 1000 + Object.values(s.analysis[f] ?? {}).filter((x) => x === "ready" || x === "error").length, 0),
  );
  const refresh = useDebounced(memberKey, 600);
  const q = useAsync(
    async (signal) => {
      const sl = await api.setlist(id, { signal, quiet: true });
      setFolders(sl.blocks.map((b) => b.folder));
      for (const b of sl.blocks) {
        if (!b.tracks.length) continue;
        useApp.getState().seedAnalysis(b.folder, b.tracks);
        void useApp.getState().loadStems(b.folder);
      }
      return sl;
    },
    [id, version, refresh],
  );
  const [pendingOrder, setPendingOrder] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const setlist: Setlist | undefined = useMemo(() => {
    const d = q.data;
    if (!d || !pendingOrder) return d;
    const by = new Map(d.blocks.map((b) => [b.folder, b]));
    return { ...d, blocks: pendingOrder.map((f) => by.get(f)).filter((b): b is NonNullable<typeof b> => !!b) };
  }, [q.data, pendingOrder]);

  const run = useCallback(
    async (what: string, fn: () => Promise<Setlist>) => {
      setBusy(what);
      try {
        const sl = await fn();
        q.setData(sl);
        return sl;
      } catch (e) {
        toast.error((e as Error).message, { title: "Setlist not updated", key: "setlist" });
        return null;
      } finally {
        setBusy(null);
      }
    },
    [q],
  );

  const reorder = useCallback(
    async (crates: string[]) => {
      setPendingOrder(crates);
      await run("order", () => api.updateSetlist(id, { crates }, { quiet: true }));
      setPendingOrder(null);
    },
    [id, run],
  );

  return {
    setlist,
    error: q.error,
    loading: q.loading,
    reload: q.reload,
    busy,
    reorder,
    rename: (name: string) => run("name", () => api.updateSetlist(id, { name }, { quiet: true })),
    setCrates: (crates: string[]) => run("crates", () => api.updateSetlist(id, { crates }, { quiet: true })),
    autoOrder: () => run("auto", () => api.autoOrderSetlist(id, { quiet: true })),
  };
}
