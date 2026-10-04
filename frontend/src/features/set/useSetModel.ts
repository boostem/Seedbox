/**
 * The crate's set: loads GET /set (refetching on set_changed / analysis bursts via useSetRefreshKey), and
 * re-orders optimistically (PUT /order) with an undo stack, auto-order, 409 ("download running") handling.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "@/api/client";
import type { CrateSet, Transition } from "@/api/types";
import { useAsync } from "@/lib/useAsync";
import { useApp, useSetRefreshKey } from "@/store/app";
import { toast } from "@/store/toasts";
import { moveBy, orderOf, pairKey, placeNextTo, sameOrder, withOrder } from "./model";

export type SaveState = "idle" | "saving" | "saved" | "error";

export interface SetModel {
  set: CrateSet | undefined;
  error: Error | null;
  loading: boolean;
  reload: () => void;
  /** Save a new running order (optimistic). */
  reorder: (ids: string[]) => Promise<void>;
  moveBy: (id: string, delta: number) => void;
  placeNextTo: (id: string, anchor: string, where: "after" | "before") => void;
  autoOrder: () => Promise<void>;
  undo: () => void;
  canUndo: boolean;
  autoOrdering: boolean;
  save: SaveState;
  /** Session cache of every transition seen (set + partner lookups), by "from>to". */
  transitionCache: Map<string, Transition>;
  rememberTransitions: (ts: Transition[]) => void;
}

const OVERRIDE_GRACE_MS = 2500; // keep showing our order while slower refetches (started before the PUT) land

function conflictToast(): void {
  toast.warning("Seedbox is still downloading into this crate. Re-order once it has finished; the set keeps its current order.", { title: "Can't re-order yet", key: "set-order-409" });
}

export function useSetModel(folder: string): SetModel {
  const refreshKey = useSetRefreshKey(folder);
  const q = useAsync((signal) => api.crateSet(folder, { signal, quiet: true }), [folder, refreshKey]);
  const { setData } = q;
  const cache = useRef(new Map<string, Transition>());
  const [override, setOverride] = useState<{ ids: string[]; until: number; saving: boolean } | null>(null);
  const [undoStack, setUndoStack] = useState<string[][]>([]);
  const undoRef = useRef(undoStack);
  undoRef.current = undoStack;
  const seq = useRef(0);
  const [save, setSave] = useState<SaveState>("idle");
  const [autoOrdering, setAutoOrdering] = useState(false);

  const rememberTransitions = useCallback((ts: Transition[]) => {
    for (const t of ts) cache.current.set(pairKey(t.from_id, t.to_id), t);
  }, []);

  useEffect(() => {
    if (!q.data) return;
    rememberTransitions(q.data.transitions);
    useApp.getState().seedAnalysis(folder, q.data.tracks);
    void useApp.getState().loadStems(folder);
  }, [q.data, folder, rememberTransitions]);

  // drop the override once its grace period is over and no save is in flight
  useEffect(() => {
    if (!override || override.saving) return;
    const ms = override.until - Date.now();
    const t = setTimeout(() => setOverride((o) => (o === override ? null : o)), Math.max(0, ms));
    return () => clearTimeout(t);
  }, [override]);

  const set = useMemo(() => (q.data && override ? withOrder(q.data, override.ids, cache.current) : q.data), [q.data, override]);
  const setRef = useRef(set);
  setRef.current = set;

  const apply = useCallback(
    async (ids: string[], opts: { fromUndo?: boolean } = {}) => {
      const current = orderOf(setRef.current);
      if (!current.length || sameOrder(ids, current)) return;
      const my = ++seq.current;
      setOverride({ ids, until: Infinity, saving: true });
      setSave("saving");
      if (!opts.fromUndo) setUndoStack((s) => [...s.slice(-29), current]);
      try {
        const next = await api.saveOrder(folder, ids, { quiet: true });
        if (my !== seq.current) return;
        rememberTransitions(next.transitions);
        setData(next);
        setOverride({ ids: orderOf(next), until: Date.now() + OVERRIDE_GRACE_MS, saving: false });
        setSave("saved");
      } catch (e) {
        if (my !== seq.current) return;
        setOverride(null);
        setSave("error");
        if (!opts.fromUndo) setUndoStack((s) => s.slice(0, -1));
        if (e instanceof ApiError && e.isConflict) conflictToast();
        else toast.error((e as Error).message, { title: "Couldn't save the order", key: "set-order" });
      }
    },
    [folder, setData, rememberTransitions],
  );

  const undo = useCallback(() => {
    const stack = undoRef.current;
    const prev = stack[stack.length - 1];
    if (!prev) return;
    setUndoStack(stack.slice(0, -1));
    void apply(prev, { fromUndo: true });
  }, [apply]);

  const autoOrder = useCallback(async () => {
    const before = orderOf(setRef.current);
    if (before.length < 2) return;
    const my = ++seq.current;
    setAutoOrdering(true);
    setSave("saving");
    try {
      const next = await api.autoOrder(folder, { quiet: true });
      if (my !== seq.current) return;
      rememberTransitions(next.transitions);
      setData(next);
      const after = orderOf(next);
      setOverride({ ids: after, until: Date.now() + OVERRIDE_GRACE_MS, saving: false });
      setSave("saved");
      const moved = after.filter((id, i) => before[i] !== id).length;
      if (moved) {
        setUndoStack((s) => [...s.slice(-29), before]);
        toast.success(`${moved} track${moved === 1 ? "" : "s"} moved · TRCK numbers and playlists rewritten.`, {
          title: "Auto-ordered",
          key: "set-auto",
          action: { label: "Undo", onClick: () => undoRef.current.length && undo() },
        });
      } else toast.info("The set is already in the suggested order.", { title: "Auto-order", key: "set-auto" });
    } catch (e) {
      if (my !== seq.current) return;
      setSave("idle"); // nothing changed: the set keeps its saved order (the toast says why)
      if (e instanceof ApiError && e.isConflict) conflictToast();
      else toast.error(`${(e as Error).message}. The set keeps its current order.`, { title: "Auto-order failed", key: "set-auto" });
    } finally {
      setAutoOrdering(false);
    }
  }, [folder, setData, rememberTransitions, undo]);

  return {
    set,
    error: q.error,
    loading: q.loading,
    reload: q.reload,
    reorder: apply,
    moveBy: (id, delta) => void apply(moveBy(orderOf(setRef.current), id, delta)),
    placeNextTo: (id, anchor, where) => void apply(placeNextTo(orderOf(setRef.current), id, anchor, where)),
    autoOrder,
    undo,
    canUndo: undoStack.length > 0,
    autoOrdering,
    save,
    transitionCache: cache.current,
    rememberTransitions,
  };
}
