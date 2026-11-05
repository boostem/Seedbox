import { ListMusic, ListOrdered, Pencil, RotateCcw, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { ArcTemplate, AssistantDraft, SetOrderResult, SetOrderSettings, SetOrderTrack, SetSummary, SetTimelineData } from "@/api/types";
import { player } from "@/audio/player";
import { Button, EmptyState, Field, IconButton, Input, Page, PageBody, PageHeader, Panel, PanelHeader, Select, Skeleton, Tabs } from "@/components/ui";
import { AssistantBox } from "@/features/assistant/AssistantBox";
import { TransitionEditor } from "@/features/mix/TransitionEditor";
import { ARC_GRID, arcPoints, toGrid, toPoints } from "@/features/sets/arc";
import { BridgePanel } from "@/features/sets/BridgePanel";
import { OrderList } from "@/features/sets/OrderList";
import { layoutTimeline, type PlacedTransition } from "@/features/sets/timeline/layout";
import { SetPlayback } from "@/features/sets/timeline/playback";
import { SetTimeline } from "@/features/sets/timeline/SetTimeline";
import { navigate } from "@/lib/router";
import { toast } from "@/store/toasts";

function missingNote(u: SetOrderResult["unknown"], total: number): string | null {
  const parts = [
    u.key && `key (${u.key})`,
    u.bpm && `tempo (${u.bpm})`,
    u.energy && `energy (${u.energy})`,
    u.vibe && `vibe (${u.vibe})`,
  ].filter(Boolean);
  return total && parts.length ? `Not analysed yet: ${parts.join(", ")}. The order ignores what it doesn't know.` : null;
}

const sameTracks = (a: { hash: string }[], b: { hash: string }[]): boolean => {
  const names = (l: { hash: string }[]) => l.map((t) => t.hash).sort().join();
  return names(a) === names(b);
};

const number = (s: string): number | null => (s.trim() === "" || !Number.isFinite(Number(s)) ? null : Number(s));

const PLANNING_POLL_MS = 5000;
const REFRESH_MS = 150; // a set's worth of plans finishing together is one request
const CUSTOM = "custom";

/** The timeline's data for this running order: asking starts the plans it lacks, and it asks again as they finish. */
function useTimelineData(setId: number | null, hashes: string[]): { data: SetTimelineData | null; refresh: () => void; retry: () => void } {
  const [data, setData] = useState<SetTimelineData | null>(null);
  const key = hashes.join(" ");
  const asked = useRef(0);
  const ask = useCallback(
    (retry = false) => {
      if (setId === null || !key) return;
      const mine = ++asked.current;
      api.sets.timeline(setId, key.split(" "), retry, { quiet: true }).then(
        (d) => {
          if (mine === asked.current) setData(d);
        },
        () => {}, // a track went from the set, or the server's away: what's on screen stays, marked by the next answer
      );
    },
    [setId, key],
  );
  useEffect(() => ask(), [ask]);
  const unfinished = data?.transitions.some((t) => t.state === "pending" || t.state === "waiting") ?? false;
  useEffect(() => {
    if (!unfinished) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(() => ask(), REFRESH_MS);
    };
    const off = onServerEvent((ev) => {
      if (ev.type !== "job.done" && ev.type !== "job.failed") return;
      const kind = (ev.data as { kind?: string } | null)?.kind;
      if (kind === "set_plan" || kind === "beats") soon();
    });
    const poll = setInterval(() => ask(), PLANNING_POLL_MS); // the events may be away
    return () => {
      off();
      clearInterval(poll);
      clearTimeout(timer);
    };
  }, [unfinished, ask]);
  return { data, refresh: useCallback(() => ask(), [ask]), retry: useCallback(() => ask(true), [ask]) };
}

function planningNote(data: SetTimelineData | null): string | null {
  const count = (state: string) => data?.transitions.filter((t) => t.state === state).length ?? 0;
  const pending = count("pending");
  const waiting = count("waiting");
  const parts = [pending && `Planning ${pending} transition${pending === 1 ? "" : "s"}…`, waiting && `${waiting} waiting for beats`].filter(Boolean);
  return parts.length ? parts.join(", ") : null;
}

export function SetsPage() {
  const [sets, setSets] = useState<SetSummary[] | null>(null);
  const [arcs, setArcs] = useState<ArcTemplate[]>([]);
  const [setId, setSetId] = useState<number | null>(null);
  const [saved, setSaved] = useState<SetOrderResult | null>(null);
  const [proposal, setProposal] = useState<SetOrderResult | null>(null);
  const [arc, setArc] = useState<SetOrderSettings["arc"]>(null);
  const [tempoStart, setTempoStart] = useState("");
  const [tempoEnd, setTempoEnd] = useState("");
  const [pins, setPins] = useState<SetOrderSettings["pins"]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [draft, setDraft] = useState<AssistantDraft | null>(null);
  const [busy, setBusy] = useState<"order" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [bridgeAt, setBridgeAt] = useState<number | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [editing, setEditing] = useState<{ position: number; from: string; to: string } | null>(null);
  const reorders = useRef(0);

  const fillFrom = useCallback((s: SetOrderSettings) => {
    setArc(s.arc);
    setTempoStart(s.tempo_start === null ? "" : String(s.tempo_start));
    setTempoEnd(s.tempo_end === null ? "" : String(s.tempo_end));
    setPins(s.pins);
    setNote(s.note ?? null);
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([api.sets.list({ quiet: true, signal: ctrl.signal }), api.sets.arcs({ quiet: true, signal: ctrl.signal })]).then(
      ([list, templates]) => {
        setSets(list);
        setArcs(templates);
        setSetId((id) => id ?? list[0]?.id ?? null);
      },
      (e) => {
        if (!ctrl.signal.aborted) setError((e as Error).message);
      },
    );
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    if (setId === null) return;
    const ctrl = new AbortController();
    setSaved(null);
    setProposal(null);
    setBridgeAt(null);
    setDraft(null);
    setError(null);
    setEditing(null);
    setDrawing(false);
    api.sets.order(setId, { quiet: true, signal: ctrl.signal }).then(
      (r) => {
        setSaved(r);
        fillFrom(r.settings);
      },
      (e) => {
        if (!ctrl.signal.aborted) setError((e as Error).message);
      },
    );
    return () => ctrl.abort();
  }, [setId, fillFrom]);

  const settings = useCallback((): SetOrderSettings => {
    const start = number(tempoStart);
    const end = number(tempoEnd);
    return { arc, pins, tempo_start: start !== null && end !== null ? start : null, tempo_end: start !== null && end !== null ? end : null, ...(note ? { note } : {}) };
  }, [arc, pins, tempoStart, tempoEnd, note]);

  const shown = proposal ?? saved;
  /** A draft with other tracks than the open set's: it can only become a set of its own. */
  const newSet = draft !== null && (saved === null || !sameTracks(draft.result.order, saved.order));

  const takeDraft = (d: AssistantDraft) => {
    setDraft(d);
    setProposal(d.result);
    fillFrom(d.result.settings);
  };
  const pinned = useMemo(() => new Set(pins.map((p) => p.position)), [pins]);
  const hashes = useMemo(() => shown?.order.map((t) => t.hash) ?? [], [shown]);
  const { data: timeline, refresh: refreshTimeline, retry: planAgain } = useTimelineData(setId, hashes);
  const layout = useMemo(() => layoutTimeline(shown?.order ?? [], shown?.transitions ?? [], timeline), [shown, timeline]);
  const failedPlans = timeline?.transitions.some((t) => t.state === "failed") ?? false;

  const playback = useMemo(
    () =>
      new SetPlayback({
        engine: async () => new (await import("@/audio/engine")).LiveEngine(),
        plan: async (from, to) => (await api.mixes.livePlan(from, to, { quiet: true })).plan,
        beforePlay: () => player.stop(),
      }),
    [],
  );
  useEffect(() => () => void playback.close(), [playback]);
  const playedOrder = useRef("");
  useEffect(() => {
    const key = hashes.join(" ");
    playback.setLayout(layout, key === playedOrder.current);
    playedOrder.current = key;
  }, [playback, layout, hashes]);

  const openTransition = useCallback(
    (t: PlacedTransition) => {
      if (t.state !== "ready") {
        toast.info(t.state === "failed" ? `Couldn't plan this transition: ${t.error ?? "unknown error"}` : "This transition is still being planned");
        return;
      }
      playback.pause();
      setEditing({ position: t.position, from: t.from, to: t.to });
    },
    [playback],
  );

  /** The editor saves as it goes, and a plan's length is where the tracks sit. */
  const closeEditor = () => {
    setEditing(null);
    refreshTimeline();
  };

  /** A track dragged on the timeline: it's there at once, and saved. Its new transitions plan in the background. */
  const reorder = (next: SetOrderTrack[]) => {
    if (setId === null || !saved) return;
    const before = saved;
    const mine = ++reorders.current;
    setEditing(null);
    setSaved({ ...saved, order: next.map((t, position) => ({ ...t, position })), transitions: [] });
    api.sets.applyOrder(setId, next.map((t) => t.hash), settings()).then(
      (r) => {
        if (mine === reorders.current) setSaved(r);
      },
      () => {
        if (mine === reorders.current) setSaved(before); // the client has shown why
      },
    );
  };

  const togglePin = (position: number) => {
    const track = shown?.order[position];
    if (!track) return;
    setPins((p) => (p.some((x) => x.position === position) ? p.filter((x) => x.position !== position) : [...p, { position, hash: track.hash }]));
  };

  const order = async () => {
    if (setId === null) return;
    setBusy("order");
    try {
      const found = await api.sets.solveOrder(setId, settings());
      setEditing(null);
      setProposal(found);
      setBridgeAt(null);
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  const keepAsNewSet = async () => {
    if (!draft) return;
    setBusy("apply");
    try {
      const created = await api.sets.create(draft.name, draft.result.order.map((t) => t.hash));
      await api.sets.applyOrder(created.id, draft.result.order.map((t) => t.hash), settings());
      setSets((list) => [...(list ?? []), created]);
      setSetId(created.id); // loads it like any other set
      toast.success(`Saved “${draft.name}”`);
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  const apply = async () => {
    if (!proposal) return;
    if (newSet) return keepAsNewSet();
    if (setId === null) return;
    setBusy("apply");
    try {
      setSaved(await api.sets.applyOrder(setId, proposal.order.map((t) => t.hash), settings()));
      setProposal(null);
      setDraft(null);
      toast.success("Saved the new order");
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  const bridged = (r: SetOrderResult) => {
    setSaved(r);
    setPins(r.settings.pins);
    setBridgeAt(null);
    toast.success("Added the bridge");
  };

  const discard = () => {
    setEditing(null);
    setProposal(null);
    if (draft) {
      setDraft(null);
      if (saved) fillFrom(saved.settings);
      else setNote(null);
      return;
    }
    setPins(saved?.settings.pins ?? []);
  };

  const dismissNote = () => {
    setNote(null);
    if (proposal || setId === null || !saved) return; // goes with the order when it's kept
    const { note: _gone, ...rest } = saved.settings;
    void api.sets.applyOrder(setId, saved.order.map((t) => t.hash), rest).then(setSaved, () => {});
  };

  if (sets && sets.length === 0 && !draft) {
    return (
      <Page>
        <PageHeader title="Sets" />
        <PageBody className="flex flex-col gap-3">
          <AssistantBox onDraft={takeDraft} className="max-w-4xl" />
          <EmptyState
            icon={ListMusic}
            title="No sets yet"
            description="Select tracks in the library and add them to a new set, then come back to put them in order."
            action={
              <Button variant="primary" onClick={() => navigate({ name: "library" })}>
                Go to the library
              </Button>
            }
          />
        </PageBody>
      </Page>
    );
  }

  const missing = shown && missingNote(shown.unknown, shown.order.length);
  const points = arcPoints(arc, arcs);
  const arcTab = Array.isArray(arc) ? CUSTOM : (arc ?? "peak_time");
  const arcItems = [...arcs.map((t) => ({ id: t.name, label: t.label, title: t.description })), { id: CUSTOM, label: "Custom", title: "Draw on the timeline" }];
  const chooseArc = (id: string) => {
    if (id === CUSTOM) {
      setArc(toPoints(toGrid(points, ARC_GRID)));
      setDrawing(true);
    } else {
      setArc(id);
      setDrawing(false);
    }
  };
  const saving = shown && proposal ? proposal.cost_before - proposal.cost : 0;

  return (
    <Page>
      <PageHeader
        title="Sets"
        subtitle={shown ? `${shown.order.length} tracks` : undefined}
        actions={
          sets && sets.length > 0 && (
            <Select aria-label="Set" value={setId ?? ""} onChange={(e) => setSetId(Number(e.target.value))}>
              {sets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          )
        }
      />
      <PageBody>
        {error ? (
          <EmptyState icon={ListOrdered} title="Couldn't load this set" description={error} />
        ) : !shown ? (
          <div className="flex max-w-4xl flex-col gap-3">
            <Skeleton className="h-48" />
            <Skeleton className="h-64" />
          </div>
        ) : (
          <div className="flex items-start gap-3">
          <div className="flex w-full min-w-0 flex-1 flex-col gap-3">
            <AssistantBox set={setId !== null ? { id: setId, name: sets?.find((s) => s.id === setId)?.name ?? "" } : null} onDraft={takeDraft} />
            <Panel>
              <PanelHeader
                title="Timeline"
                description={planningNote(timeline) ?? (proposal ? "The proposed order: use it or keep the saved one to drag tracks again." : "Drag a track to move it. Click a transition to edit it, anywhere else to play from there.")}
                actions={
                  failedPlans ? (
                    <Button size="sm" onClick={planAgain}>
                      Plan the failed transitions again
                    </Button>
                  ) : undefined
                }
              />
              <div className="flex flex-col gap-3 p-4">
                <SetTimeline
                  key={setId ?? "none"}
                  layout={layout}
                  arc={points}
                  onArc={setArc}
                  drawing={drawing}
                  reorderable={!proposal}
                  pinned={pinned}
                  onReorder={reorder}
                  open={editing?.position ?? null}
                  onOpen={openTransition}
                  playback={playback}
                />
                <div className="flex flex-wrap items-center gap-2">
                  <Tabs variant="segmented" items={arcItems} value={arcTab} onChange={chooseArc} />
                  <Button size="sm" icon={Pencil} variant={drawing ? "primary" : "secondary"} aria-pressed={drawing} onClick={() => setDrawing((d) => !d)}>
                    Draw the arc
                  </Button>
                </div>
                <div className="flex flex-wrap items-end gap-3">
                  <Field label="Tempo from" hint={shown.tempo?.auto ? `Empty: ${shown.tempo.start.toFixed(0)} from this set` : undefined} className="w-40">
                    <Input inputMode="decimal" suffix="BPM" value={tempoStart} onChange={(e) => setTempoStart(e.target.value)} aria-label="Tempo from" />
                  </Field>
                  <Field label="to" hint={shown.tempo?.auto ? `Empty: ${shown.tempo.end.toFixed(0)} from this set` : undefined} className="w-40">
                    <Input inputMode="decimal" suffix="BPM" value={tempoEnd} onChange={(e) => setTempoEnd(e.target.value)} aria-label="Tempo to" />
                  </Field>
                  <div className="flex-1" />
                  {proposal ? (
                    <>
                      <Button icon={RotateCcw} onClick={discard}>
                        {newSet ? "Discard this draft" : "Keep the saved order"}
                      </Button>
                      {!newSet && (
                        <Button icon={ListOrdered} onClick={order} loading={busy === "order"}>
                          Order again
                        </Button>
                      )}
                      <Button variant="primary" onClick={apply} loading={busy === "apply"}>
                        {newSet ? "Keep as a new set" : "Use this order"}
                      </Button>
                    </>
                  ) : (
                    <Button variant="primary" icon={Sparkles} onClick={order} loading={busy === "order"} disabled={shown.order.length < 2}>
                      Order the set
                    </Button>
                  )}
                </div>
              </div>
            </Panel>
            {editing && (
              <Panel>
                <PanelHeader
                  title={`${shown.order[editing.position]?.title ?? ""} → ${shown.order[editing.position + 1]?.title ?? ""}`}
                  actions={<IconButton size="sm" icon={X} label="Close the transition" onClick={closeEditor} />}
                />
                <TransitionEditor key={`${editing.from}/${editing.to}`} from={editing.from} to={editing.to} />
              </Panel>
            )}
            <Panel>
              <PanelHeader
                title={newSet ? `Draft: ${draft?.name}` : proposal ? "Proposed order" : "Running order"}
                description={newSet ? `${shown.order.length} tracks chosen by the assistant. Keep it to edit it like any other set.` : proposal ? (saving > 0.0005 ? `Smoother than the saved order: cost ${proposal.cost_before.toFixed(1)} → ${proposal.cost.toFixed(1)}` : "The saved order is already the best one found") : missing ?? undefined}
              />
              {proposal && missing && <p className="border-b border-line/60 px-4 py-1.5 text-xs text-fg-subtle">{missing}</p>}
              {shown.order.length ? (
                <OrderList
                  result={shown}
                  pinned={pinned}
                  onPin={togglePin}
                  onOpen={(position) => layout.transitions[position] && openTransition(layout.transitions[position])}
                  onBridge={proposal ? undefined : (p) => setBridgeAt((at) => (at === p ? null : p))}
                  bridgeAt={proposal ? null : bridgeAt}
                  bridgePanel={setId !== null && bridgeAt !== null && <BridgePanel key={`${setId}-${bridgeAt}`} setId={setId} position={bridgeAt} onInserted={bridged} />}
                />
              ) : (
                <EmptyState icon={ListMusic} title="This set is empty" description="Add tracks to it from the library." />
              )}
            </Panel>
          </div>
          {note && (
            <aside aria-label="The assistant's reasoning" className="w-64 shrink-0 rounded-md border border-line bg-panel p-3">
              <div className="mb-1 flex items-center justify-between gap-2">
                <h2 className="text-xs font-medium text-fg">The assistant's reasoning</h2>
                <IconButton size="sm" icon={X} label="Dismiss the assistant's reasoning" onClick={dismissNote} />
              </div>
              <p className="whitespace-pre-wrap text-xs leading-relaxed text-fg-muted">{note}</p>
            </aside>
          )}
          </div>
        )}
      </PageBody>
    </Page>
  );
}
