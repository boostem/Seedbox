import { Download, FolderOpen, Upload } from "lucide-react";
import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ListenRow, ListenSummary } from "@/api/types";
import { Badge, Button, EmptyState, Panel, PanelHeader, Spinner, Table, TBody, Td, Th, THead, Tr } from "@/components/ui";
import { toast } from "@/store/toasts";
import { interval, verdict, winRate } from "./rating";

function RateBar({ low, high, rate }: { low: number; high: number; rate: number | null }) {
  if (rate == null) return null;
  return (
    <div className="relative h-2 w-32 rounded-full bg-overlay" aria-hidden>
      <div className="absolute inset-y-0 rounded-full bg-fg-faint" style={{ left: `${low * 100}%`, width: `${(high - low) * 100}%` }} />
      <div className="absolute inset-y-0 w-px bg-fg-subtle" style={{ left: "50%" }} />
      <div className="absolute inset-y-0 w-0.5 bg-fg" style={{ left: `${rate * 100}%` }} />
    </div>
  );
}

function Verdict({ row }: { row: { win_rate: number | null; ci_low: number; ci_high: number; trials: number; wins: number } }) {
  const v = verdict(row);
  if (v === "better") return <Badge tone="ok">Better than a coin flip</Badge>;
  if (v === "worse") return <Badge tone="warn">Worse than a coin flip</Badge>;
  return <span className="text-xs text-fg-subtle">Not clear yet</span>;
}

const mean = (x: number | undefined) => (x == null ? "–" : x.toFixed(1));

function RateTable({ title, description, rows, empty }: { title: string; description: string; rows: ListenRow[]; empty: string }) {
  return (
    <Panel>
      <PanelHeader title={title} description={description} />
      {rows.length === 0 ? <p className="p-4 text-sm text-fg-subtle">{empty}</p> : (
        <Table>
          <THead>
            <Tr>
              <Th>Name</Th>
              <Th align="right">Win rate</Th>
              <Th>95% interval</Th>
              <Th />
              <Th align="right">Wins</Th>
              <Th align="right">Smooth</Th>
              <Th align="right">Energy</Th>
              <Th align="right">DJ</Th>
              <Th />
            </Tr>
          </THead>
          <TBody>
            {rows.map((r) => (
              <Tr key={r.name}>
                <Td className="font-medium">{r.name}</Td>
                <Td num align="right">{winRate(r)}</Td>
                <Td num>{interval(r)}</Td>
                <Td><RateBar low={r.ci_low} high={r.ci_high} rate={r.win_rate} /></Td>
                <Td num align="right">{r.wins} of {r.trials}</Td>
                <Td num align="right">{mean(r.scores.smoothness)}</Td>
                <Td num align="right">{mean(r.scores.energy)}</Td>
                <Td num align="right">{mean(r.scores.dj)}</Td>
                <Td><Verdict row={r} /></Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
    </Panel>
  );
}

/** The results, with what it takes to pool them with other people's. This tab is the only place engines are named. */
export function ResultsPanel({ rater, onChange }: { rater: string; onChange: () => void }) {
  const [summary, setSummary] = useState<ListenSummary | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  const load = useCallback(() => api.listen.summary().then(setSummary, () => {}), []);
  useEffect(() => void load(), [load]);

  const exportFile = async () => {
    setBusy(true);
    try {
      const { path, ratings } = await api.listen.saveExport(rater);
      setSaved(path);
      toast.success(`Saved ${ratings} rating${ratings === 1 ? "" : "s"}. Send the file to whoever pools the results.`);
    } catch {
      /* the client has shown it */
    }
    setBusy(false);
  };

  const importFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = ""; // so the same file can be picked again
    if (!f) return;
    setBusy(true);
    try {
      let doc: unknown;
      try {
        doc = JSON.parse(await f.text());
      } catch {
        toast.error(`${f.name} isn't a ratings file: it isn't JSON.`);
        return;
      }
      const r = await api.listen.import(doc);
      toast.success(`Added ${r.added} rating${r.added === 1 ? "" : "s"}${r.duplicates ? `, ${r.duplicates} already here` : ""}.`);
      await load();
      onChange();
    } catch {
      /* the client has shown it */
    } finally {
      setBusy(false);
    }
  };

  if (!summary) return <div className="flex justify-center p-8"><Spinner /></div>;
  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <PanelHeader
          title="Pool results"
          description={`${summary.ratings} rating${summary.ratings === 1 ? "" : "s"} from ${summary.raters} ${summary.raters === 1 ? "person" : "people"}. Files hold ratings only, no audio, and importing one twice adds nothing.`}
          actions={
            <div className="flex items-center gap-2">
              <input ref={file} type="file" accept=".json,application/json" className="hidden" aria-label="Ratings file to import" onChange={(e) => void importFile(e)} />
              <Button icon={Upload} disabled={busy} onClick={() => file.current?.click()}>Import</Button>
              <Button icon={Download} disabled={busy || summary.ratings === 0} onClick={() => void exportFile()}>Export</Button>
            </div>
          }
        />
        {saved && (
          <div className="flex items-center gap-2 p-4 text-sm text-fg-muted">
            <span className="min-w-0 truncate font-mono">{saved}</span>
            <Button size="xs" variant="ghost" icon={FolderOpen} onClick={() => void api.reveal(saved)}>Show</Button>
          </div>
        )}
      </Panel>
      {summary.ratings === 0 ? <EmptyState title="No ratings yet" description="Rate a few comparisons, or import someone's ratings." /> : (
        <>
          <RateTable title="Engine versions" description="Trials between two different versions only." rows={summary.versions} empty="No trial has compared two versions yet." />
          <RateTable title="Styles" description="Trials between two different styles only." rows={summary.styles} empty="No trial has compared two styles yet." />
          {summary.matchups.length > 0 && (
            <Panel>
              <PanelHeader title="Head to head" description="A win rate depends on the opponents; this doesn't." />
              <Table>
                <THead>
                  <Tr>
                    <Th>Version</Th>
                    <Th>Against</Th>
                    <Th align="right">Win rate</Th>
                    <Th>95% interval</Th>
                    <Th />
                    <Th align="right">Wins</Th>
                    <Th />
                  </Tr>
                </THead>
                <TBody>
                  {summary.matchups.map((m) => (
                    <Tr key={`${m.name}|${m.opponent}`}>
                      <Td className="font-medium">{m.name}</Td>
                      <Td>{m.opponent}</Td>
                      <Td num align="right">{winRate(m)}</Td>
                      <Td num>{interval(m)}</Td>
                      <Td><RateBar low={m.ci_low} high={m.ci_high} rate={m.win_rate} /></Td>
                      <Td num align="right">{m.wins} of {m.trials}</Td>
                      <Td><Verdict row={m} /></Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            </Panel>
          )}
          <p className="text-xs text-fg-subtle">
            Sanity check: the version played as A won {winRate(summary.slot_a)} of the time ({interval(summary.slot_a)}). Far from 50%, the order is biasing people. Intervals are Wilson 95%; answers from one person aren't fully independent, so read them as a little too narrow.
          </p>
        </>
      )}
    </div>
  );
}
