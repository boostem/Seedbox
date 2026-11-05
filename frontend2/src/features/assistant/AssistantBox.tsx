/**
 * The prompt box in the set builder (#63). Not there while the assistant is off. It hands the page a draft: nothing is
 * saved until the person keeps it, and from then on it's a set like any other.
 */
import { Eye, Settings, Sparkles, Square } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { AssistantDraft, AssistantScope, AssistantStatus, CrateSummary } from "@/api/types";
import { Button, Checkbox, Panel, PanelHeader, Spinner } from "@/components/ui";
import { navigate } from "@/lib/router";
import { DryRunModal } from "./DryRunModal";

export interface AssistantBoxProps {
  /** The set that's open, which can be the only source of tracks. */
  set?: { id: number; name: string } | null;
  onDraft: (draft: AssistantDraft) => void;
  className?: string;
}

export function AssistantBox({ set, onDraft, className }: AssistantBoxProps) {
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [crates, setCrates] = useState<CrateSummary[]>([]);
  const [prompt, setPrompt] = useState("");
  const [useSet, setUseSet] = useState(false);
  const [crateIds, setCrateIds] = useState<number[]>([]);
  const [running, setRunning] = useState(false);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dryRun, setDryRun] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    api.assistant.status({ quiet: true, signal: ctrl.signal }).then(setStatus, () => {});
    return () => ctrl.abort();
  }, []);

  const ready = Boolean(status?.enabled && status.has_key);
  useEffect(() => {
    if (!ready) return;
    const ctrl = new AbortController();
    api.library.crates({ quiet: true, signal: ctrl.signal }).then(setCrates, () => {});
    return () => ctrl.abort();
  }, [ready]);

  useEffect(() => {
    if (!running) return;
    return onServerEvent((ev) => {
      if (ev.type === "assistant.step") setStep((ev.data as { text: string }).text);
    });
  }, [running]);

  const scope: AssistantScope = useMemo(() => ({ set_id: useSet && set ? set.id : undefined, crate_ids: crateIds }), [useSet, set, crateIds]);

  if (!status?.enabled) return null;
  if (!status.has_key) {
    return (
      <Panel className={className}>
        <PanelHeader
          title="Set assistant"
          description="It's on, and needs your Anthropic API key."
          actions={
            <Button size="sm" icon={Settings} onClick={() => navigate({ name: "settings" })}>
              Open Settings
            </Button>
          }
        />
      </Panel>
    );
  }

  const go = async () => {
    setRunning(true);
    setStep(null);
    setError(null);
    try {
      onDraft(await api.assistant.run({ prompt, ...scope }, { quiet: true }));
      setPrompt("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const toggleCrate = (id: number, on: boolean) => setCrateIds((ids) => (on ? [...ids, id] : ids.filter((x) => x !== id)));

  return (
    <Panel className={className}>
      <PanelHeader title="Ask the assistant" description="Describe the set. You get a draft to look over, and nothing is saved until you keep it." />
      <div className="flex flex-col gap-2 p-4">
        <textarea
          aria-label="Describe the set"
          value={prompt}
          rows={2}
          maxLength={2000}
          disabled={running}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="A 90-minute sunset deep-house set: start mellow, peak at 60 minutes, end on something vocal"
          className="w-full resize-y rounded-sm border border-line-strong bg-raised px-2 py-1.5 text-sm text-fg outline-none placeholder:text-fg-faint hover:border-fg-faint focus:border-accent"
        />
        {(set || crates.length > 0) && (
          <div className="flex max-h-20 flex-wrap items-center gap-x-4 gap-y-1 overflow-y-auto text-xs text-fg-muted">
            <span>{useSet || crateIds.length ? "Only from:" : "From your whole library, or only:"}</span>
            {set && <Checkbox size="sm" checked={useSet} onChange={setUseSet} label={`This set (${set.name})`} />}
            {crates.map((c) => (
              <Checkbox key={c.id} size="sm" checked={crateIds.includes(c.id)} onChange={(on) => toggleCrate(c.id, on)} label={c.name} />
            ))}
          </div>
        )}
        {error && (
          <p role="alert" className="text-xs text-danger">
            {error}
          </p>
        )}
        <div className="flex items-center gap-2">
          {running && (
            <span className="flex min-w-0 items-center gap-2 text-xs text-fg-muted" aria-live="polite">
              <Spinner />
              <span className="truncate">{step ?? "Asking Claude…"}</span>
            </span>
          )}
          <div className="flex-1" />
          <Button size="sm" variant="ghost" icon={Eye} onClick={() => setDryRun(true)}>
            See what would be sent
          </Button>
          {running ? (
            <Button icon={Square} onClick={() => void api.assistant.cancel({ quiet: true }).catch(() => {})}>
              Stop
            </Button>
          ) : (
            <Button variant="primary" icon={Sparkles} onClick={go} disabled={!prompt.trim()}>
              Build a draft
            </Button>
          )}
        </div>
      </div>
      <DryRunModal open={dryRun} onClose={() => setDryRun(false)} prompt={prompt} scope={scope} />
    </Panel>
  );
}
