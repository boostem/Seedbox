import { CloudDownload, CloudOff, FolderOpen, RotateCw, Trash2, X } from "lucide-react";
import { useEffect } from "react";
import { api } from "@/api/client";
import type { ModelInfo } from "@/api/types";
import { Badge, Button, IconButton, Panel, PanelHeader, ProgressBar, StatusDot, Switch } from "@/components/ui";
import { fmtBytes } from "@/lib/format";
import { useApp } from "@/store/app";
import { useModels } from "@/store/models";

const STATE: Record<ModelInfo["state"], { tone: "ok" | "warn" | "danger" | "info"; label: string }> = {
  installed: { tone: "ok", label: "Downloaded" },
  missing: { tone: "warn", label: "Not downloaded" },
  downloading: { tone: "info", label: "Downloading" },
  failed: { tone: "danger", label: "Failed" },
};

/** The OS's accelerator, or null where there is none (Linux: CPU only). */
function acceleratorName(platform: string | undefined): string | null {
  if (platform === "darwin") return "CoreML";
  if (platform === "win32") return "DirectML";
  return null;
}

function ModelRow({ m }: { m: ModelInfo }) {
  const state = STATE[m.state];
  const reload = () => void useModels.getState().refresh();
  return (
    <div className="flex flex-col gap-1.5 border-b border-line/60 px-4 py-3 last:border-b-0">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-fg">{m.title}</span>
        <span className="font-mono text-xs text-fg-faint">{m.version}</span>
        <Badge tone="neutral" size="xs">
          {m.licence}
        </Badge>
        <span className="text-xs text-fg-subtle">{fmtBytes(m.size)}</span>
        <span className="flex-1" />
        <StatusDot tone={state.tone} pulse={m.state === "downloading"} label={state.label} />
        {m.state === "downloading" ? (
          <IconButton icon={X} label="Cancel" tooltip="Cancel (resumes where it stopped next time)" size="sm" onClick={() => void api.models.cancel(m.name)} />
        ) : m.installed ? (
          <>
            <IconButton icon={RotateCw} label="Download again" tooltip="Download again" size="sm" onClick={() => void api.models.download(m.name, true).then(reload)} />
            <IconButton icon={Trash2} label="Delete" tooltip={`Delete (frees ${fmtBytes(m.size)})`} size="sm" onClick={() => void api.models.remove(m.name).then(reload)} />
          </>
        ) : (
          <Button size="sm" icon={CloudDownload} onClick={() => void api.models.download(m.name).then(reload)}>
            {m.done > 0 ? "Resume" : "Download"}
          </Button>
        )}
      </div>
      {m.state === "downloading" && (
        <div className="flex items-center gap-2">
          <ProgressBar className="flex-1" value={m.size ? m.done / m.size : null} label={`${m.title} download`} />
          <span className="w-32 text-right font-mono text-xs text-fg-subtle">
            {fmtBytes(m.done)} / {fmtBytes(m.size)}
          </span>
        </div>
      )}
      <p className="text-xs text-fg-muted">
        {m.used_for}. Without it: {m.fallback}.
      </p>
      <p className="text-xs text-fg-subtle">
        {m.attribution}{" "}
        <a className="text-accent-hover hover:underline" href={m.homepage} target="_blank" rel="noreferrer">
          {m.homepage.replace(/^https?:\/\//, "")}
        </a>
      </p>
      {m.state === "failed" && m.error && <p className="text-xs text-danger">{m.error}</p>}
    </div>
  );
}

/** Settings → Models (#42): what is downloaded, sizes, licences and credits; download, re-download, delete. */
export function ModelsPanel() {
  const status = useModels((s) => s.status);
  const system = useApp((s) => s.system);
  useEffect(() => void useModels.getState().refresh(), []);
  const accel = acceleratorName(system?.platform);
  const setSetting = (changes: Parameters<typeof api.models.settings>[0]) => void api.models.settings(changes).then((s) => useModels.getState().set(s));
  const missing = status?.missing ?? [];
  const busy = status?.models.some((m) => m.state === "downloading");

  return (
    <Panel className="max-w-3xl">
      <PanelHeader
        title="Models"
        description={
          status
            ? `onnxruntime models, downloaded once into the cache folder and checked by SHA-256: ${fmtBytes(status.installed_bytes)} of ${fmtBytes(status.total_bytes)}`
            : "onnxruntime models, downloaded once into the cache folder and checked by SHA-256"
        }
        actions={
          <>
            {status && system && <IconButton icon={FolderOpen} label={system.reveal_label} tooltip={system.reveal_label} size="sm" onClick={() => void api.reveal(status.dir, { quiet: true })} />}
            {missing.length > 0 && (
              <Button size="sm" icon={CloudDownload} disabled={busy} onClick={() => void api.models.downloadMissing().then((s) => useModels.getState().set(s))}>
                Download all
              </Button>
            )}
          </>
        }
      />
      {missing.length > 0 && (
        <div className="flex gap-3 border-b border-line bg-warn/8 px-4 py-2.5">
          <CloudOff size={16} className="mt-0.5 shrink-0 text-warn" />
          <div className="text-xs text-fg-muted">
            <p className="font-medium text-fg">Seedbox 2 works without these, on the Seedbox 1 heuristics:</p>
            <ul className="mt-1 list-disc pl-4">
              {missing.map((m) => (
                <li key={m.name}>
                  {m.title} ({m.used_for.toLowerCase()}): using {m.fallback} until it is downloaded
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {status?.models.map((m) => <ModelRow key={m.name} m={m} />)}
      {status && (
        <div className="flex flex-col gap-2 border-t border-line px-4 py-2.5">
          <Switch size="sm" checked={status.settings.auto_download} onChange={(v) => setSetting({ auto_download: v })} label="Download missing models when Seedbox 2 starts" />
          <Switch
            size="sm"
            checked={accel !== null && status.settings.accelerator}
            disabled={accel === null}
            onChange={(v) => setSetting({ accelerator: v })}
            label={accel ? `Use ${accel} for models (the CPU stays the fallback; applies to new workers)` : "Hardware acceleration: none on this OS, models run on the CPU"}
          />
        </div>
      )}
    </Panel>
  );
}
