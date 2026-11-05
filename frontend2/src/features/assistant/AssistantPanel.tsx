/** Settings → Set assistant (#63): off by default; the API key goes to the OS keychain, and never comes back. */
import { Eye, KeyRound, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { AssistantStatus } from "@/api/types";
import { Button, InfoRow, Input, Panel, PanelHeader, Select, Switch } from "@/components/ui";
import { DryRunModal } from "./DryRunModal";
import { PrivacyNote } from "./PrivacyNote";

const EXAMPLE = "Make me a 90-minute sunset deep-house set, start mellow, peak at 60 minutes, end on something vocal.";
const WHOLE_LIBRARY = { crate_ids: [] };

export function AssistantPanel() {
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [key, setKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [dryRun, setDryRun] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    api.assistant.status({ quiet: true, signal: ctrl.signal }).then(setStatus, () => {});
    return () => ctrl.abort();
  }, []);

  const saveKey = async () => {
    setSaving(true);
    try {
      setStatus(await api.assistant.setKey(key));
      setKey("");
    } catch {
      // the client has shown it
    } finally {
      setSaving(false);
    }
  };

  const update = (change: { enabled?: boolean; model?: string }) => void api.assistant.update(change).then(setStatus, () => {});

  return (
    <Panel className="max-w-3xl">
      <PanelHeader title="Set assistant" description="Optional: describe a set in a sentence on the Sets page and Claude builds a draft from your library" />
      {status && (
        <>
          <InfoRow label="Assistant">
            <Switch checked={status.enabled} onChange={(enabled) => update({ enabled })} label={status.enabled ? "On" : "Off"} />
          </InfoRow>
          <InfoRow label="Anthropic API key">
            {status.has_key ? (
              <>
                <span className="text-fg-muted">Saved in this computer's keychain</span>
                <Button size="sm" variant="ghost" icon={Trash2} onClick={() => void api.assistant.deleteKey().then(setStatus, () => {})}>
                  Remove
                </Button>
              </>
            ) : status.keychain ? (
              <>
                <Input type="password" autoComplete="off" aria-label="Anthropic API key" placeholder="sk-ant-…" value={key} onChange={(e) => setKey(e.target.value)} className="min-w-0 flex-1" />
                <Button size="sm" icon={KeyRound} onClick={saveKey} loading={saving} disabled={!key.trim()}>
                  Save in keychain
                </Button>
              </>
            ) : (
              <span className="text-warn">This computer has no keychain to keep a key in, so the assistant can't run here.</span>
            )}
          </InfoRow>
          <InfoRow label="Model">
            <Select aria-label="Assistant model" value={status.model} onChange={(e) => update({ model: e.target.value })}>
              {status.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </Select>
          </InfoRow>
          <div className="flex items-start gap-4 px-4 py-2.5 text-xs text-fg-muted">
            <PrivacyNote className="min-w-0 flex-1" />
            <Button size="sm" variant="ghost" icon={Eye} className="shrink-0" onClick={() => setDryRun(true)}>
              See what would be sent
            </Button>
          </div>
          <DryRunModal open={dryRun} onClose={() => setDryRun(false)} prompt={EXAMPLE} scope={WHOLE_LIBRARY} />
        </>
      )}
    </Panel>
  );
}
