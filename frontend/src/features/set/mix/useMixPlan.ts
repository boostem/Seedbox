/**
 * Editable MixPlan for one A → B pair: GET (saved edits or generated), regenerate with technique/bars, live edits
 * with undo/redo (drags update transiently and commit one history step at the end), Save (PUT) / Revert (DELETE).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/api/client";
import type { MixPlan, MixTechnique } from "@/api/types";
import { toast } from "@/store/toasts";

export interface MixPlanApi {
  plan: MixPlan | null;
  loading: boolean;
  error: ApiError | null;
  /** True when the server has no mix-plan endpoints (older engine) → caller falls back to the static mix view. */
  unavailable: boolean;
  /** Differs from the last server copy (unsaved edits or a re-generated plan). */
  dirty: boolean;
  saving: boolean;
  /** Replace the plan. `transient` = live drag update (no history step); call `checkpoint` before the drag. */
  update: (p: MixPlan, opts?: { transient?: boolean }) => void;
  /** Record `snapshot` as one undo step (used at the end of a drag). */
  checkpoint: (snapshot: MixPlan) => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  regenerate: (technique: MixTechnique, bars: number) => Promise<void>;
  save: () => Promise<void>;
  revert: () => Promise<void>;
  reload: () => void;
}

const strip = (p: MixPlan | null) => (p ? JSON.stringify({ ...p, edited: false, notes: [], markers: [] }) : "");

export function useMixPlan(folder: string, from: string, to: string, enabled: boolean): MixPlanApi {
  const [plan, setPlan] = useState<MixPlan | null>(null);
  const [baseline, setBaseline] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [saving, setSaving] = useState(false);
  const past = useRef<MixPlan[]>([]);
  const future = useRef<MixPlan[]>([]);
  const [, setHist] = useState(0); // re-render when the history stacks change
  const [tick, setTick] = useState(0);
  const planRef = useRef(plan);
  planRef.current = plan;
  const bump = () => setHist((h) => h + 1);
  const pushPast = (p: MixPlan) => {
    past.current = [...past.current.slice(-99), p];
    future.current = [];
    bump();
  };

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    setLoading(true);
    api
      .mixPlan(folder, from, to, {}, { quiet: true, signal: ctrl.signal })
      .then((p) => {
        setPlan(p);
        setBaseline(strip(p));
        past.current = [];
        future.current = [];
        bump();
        setError(null);
      })
      .catch((e: unknown) => {
        if ((e as Error).name === "AbortError") return;
        setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
      })
      .finally(() => !ctrl.signal.aborted && setLoading(false));
    return () => ctrl.abort();
  }, [folder, from, to, enabled, tick]);

  const update = useCallback((p: MixPlan, opts: { transient?: boolean } = {}) => {
    if (!opts.transient && planRef.current) pushPast(planRef.current);
    planRef.current = p;
    setPlan(p);
  }, []);

  const checkpoint = useCallback((snapshot: MixPlan) => {
    if (snapshot === planRef.current) return;
    pushPast(snapshot);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const undo = useCallback(() => {
    const prev = past.current[past.current.length - 1];
    if (!prev || !planRef.current) return;
    past.current = past.current.slice(0, -1);
    future.current = [planRef.current, ...future.current];
    planRef.current = prev;
    setPlan(prev);
    bump();
  }, []);

  const redo = useCallback(() => {
    const next = future.current[0];
    if (!next || !planRef.current) return;
    future.current = future.current.slice(1);
    past.current = [...past.current, planRef.current];
    planRef.current = next;
    setPlan(next);
    bump();
  }, []);

  const regenerate = useCallback(
    async (technique: MixTechnique, bars: number) => {
      try {
        const p = await api.mixPlan(folder, from, to, { technique, bars }, { quiet: true });
        update(p);
      } catch (e) {
        toast.error((e as Error).message, { title: "Couldn't generate that mix", key: "mixplan" });
      }
    },
    [folder, from, to, update],
  );

  const save = useCallback(async () => {
    const p = planRef.current;
    if (!p) return;
    setSaving(true);
    try {
      const saved = await api.saveMixPlan(folder, from, to, p, { quiet: true });
      planRef.current = saved;
      setPlan(saved);
      setBaseline(strip(saved));
      toast.success("Mix plan saved for this transition.", { key: "mixplan" });
    } catch (e) {
      toast.error((e as Error).message, { title: "Couldn't save the mix plan", key: "mixplan" });
    } finally {
      setSaving(false);
    }
  }, [folder, from, to]);

  const revert = useCallback(async () => {
    setSaving(true);
    try {
      const p = await api.resetMixPlan(folder, from, to, { quiet: true });
      if (planRef.current) pushPast(planRef.current);
      planRef.current = p;
      setPlan(p);
      setBaseline(strip(p));
      toast.info("Back to the generated mix plan.", { key: "mixplan" });
    } catch (e) {
      toast.error((e as Error).message, { title: "Couldn't revert", key: "mixplan" });
    } finally {
      setSaving(false);
    }
  }, [folder, from, to]); // eslint-disable-line react-hooks/exhaustive-deps

  const unavailable = !!error && (error.status === 405 || error.status === 501 || (error.status === 404 && /^not found$/i.test(error.message.trim())));
  return {
    plan,
    loading,
    error,
    unavailable,
    dirty: !!plan && strip(plan) !== baseline,
    saving,
    update,
    checkpoint,
    undo,
    redo,
    canUndo: past.current.length > 0,
    canRedo: future.current.length > 0,
    regenerate,
    save,
    revert,
    reload: () => setTick((t) => t + 1),
  };
}
