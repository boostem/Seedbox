import { ListMusic, ListOrdered, RotateCcw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { ArcTemplate, SetOrderResult, SetOrderSettings, SetSummary } from "@/api/types";
import { Button, EmptyState, Field, Input, Page, PageBody, PageHeader, Panel, PanelHeader, Select, Skeleton } from "@/components/ui";
import { ArcEditor } from "@/features/sets/ArcEditor";
import { OrderList } from "@/features/sets/OrderList";
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

const number = (s: string): number | null => (s.trim() === "" || !Number.isFinite(Number(s)) ? null : Number(s));

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
  const [busy, setBusy] = useState<"order" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    setError(null);
    api.sets.order(setId, { quiet: true, signal: ctrl.signal }).then(
      (r) => {
        setSaved(r);
        setArc(r.settings.arc);
        setTempoStart(r.settings.tempo_start === null ? "" : String(r.settings.tempo_start));
        setTempoEnd(r.settings.tempo_end === null ? "" : String(r.settings.tempo_end));
        setPins(r.settings.pins);
      },
      (e) => {
        if (!ctrl.signal.aborted) setError((e as Error).message);
      },
    );
    return () => ctrl.abort();
  }, [setId]);

  const settings = useCallback((): SetOrderSettings => {
    const start = number(tempoStart);
    const end = number(tempoEnd);
    return { arc, pins, tempo_start: start !== null && end !== null ? start : null, tempo_end: start !== null && end !== null ? end : null };
  }, [arc, pins, tempoStart, tempoEnd]);

  const shown = proposal ?? saved;
  const pinned = useMemo(() => new Set(pins.map((p) => p.position)), [pins]);

  const togglePin = (position: number) => {
    const track = shown?.order[position];
    if (!track) return;
    setPins((p) => (p.some((x) => x.position === position) ? p.filter((x) => x.position !== position) : [...p, { position, hash: track.hash }]));
  };

  const order = async () => {
    if (setId === null) return;
    setBusy("order");
    try {
      setProposal(await api.sets.solveOrder(setId, settings()));
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  const apply = async () => {
    if (setId === null || !proposal) return;
    setBusy("apply");
    try {
      setSaved(await api.sets.applyOrder(setId, proposal.order.map((t) => t.hash), settings()));
      setProposal(null);
      toast.success("Saved the new order");
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  const discard = () => {
    setProposal(null);
    setPins(saved?.settings.pins ?? []);
  };

  if (sets && sets.length === 0) {
    return (
      <Page>
        <PageHeader title="Sets" />
        <PageBody>
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

  const note = shown && missingNote(shown.unknown, shown.order.length);
  const saving = shown && proposal ? proposal.cost_before - proposal.cost : 0;

  return (
    <Page>
      <PageHeader
        title="Sets"
        subtitle={shown ? `${shown.order.length} tracks` : undefined}
        actions={
          sets && (
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
          <div className="flex max-w-4xl flex-col gap-3">
            <Panel>
              <PanelHeader title="Energy and tempo" description="The line is the shape to follow, from the set's quietest track to its most intense; each bar is a track where it plays now. Drag to draw your own." />
              <div className="flex flex-col gap-3 p-4">
                <ArcEditor arc={arc} templates={arcs} tracks={shown.order} onChange={setArc} />
                <div className="flex items-end gap-3">
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
                        Keep the saved order
                      </Button>
                      <Button icon={ListOrdered} onClick={order} loading={busy === "order"}>
                        Order again
                      </Button>
                      <Button variant="primary" onClick={apply} loading={busy === "apply"}>
                        Use this order
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
            <Panel>
              <PanelHeader
                title={proposal ? "Proposed order" : "Running order"}
                description={proposal ? (saving > 0.0005 ? `Smoother than the saved order: cost ${proposal.cost_before.toFixed(1)} → ${proposal.cost.toFixed(1)}` : "The saved order is already the best one found") : note ?? undefined}
              />
              {proposal && note && <p className="border-b border-line/60 px-4 py-1.5 text-xs text-fg-subtle">{note}</p>}
              {shown.order.length ? (
                <OrderList result={shown} pinned={pinned} onPin={togglePin} />
              ) : (
                <EmptyState icon={ListMusic} title="This set is empty" description="Add tracks to it from the library." />
              )}
            </Panel>
          </div>
        )}
      </PageBody>
    </Page>
  );
}
