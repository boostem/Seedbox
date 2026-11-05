import { FolderOpen } from "lucide-react";
import { api } from "@/api/client";
import type { SystemFolders } from "@/api/types";
import { IconButton, InfoRow, Page, PageBody, PageHeader, Panel, PanelHeader, Skeleton, StatusDot } from "@/components/ui";
import { AssistantPanel } from "@/features/assistant/AssistantPanel";
import { WatchFoldersPanel } from "@/features/import/WatchFoldersPanel";
import { ModelsPanel } from "@/features/models/ModelsPanel";
import { StemsPanel } from "@/features/models/StemsPanel";
import { MasterPanel } from "@/features/system/MasterPanel";
import { PowerPanel } from "@/features/system/PowerPanel";
import { WaveSettingsPanel } from "@/features/waveform";
import { useApp } from "@/store/app";

/** A value that hasn't arrived: its outline while the engine may still answer, a word once it can't. */
function Pending({ width = "w-40" }: { width?: string }) {
  const offline = useApp((s) => s.connection === "offline");
  return offline ? <span className="text-fg-faint">unavailable</span> : <Skeleton className={width} />;
}

function Value({ v }: { v: string | null | undefined }) {
  if (v === undefined) return <Pending />;
  return v ? <span className="truncate font-mono text-xs">{v}</span> : <span className="text-fg-faint">not found</span>;
}

const FOLDERS: { key: keyof SystemFolders; label: string }[] = [
  { key: "music", label: "Music library" },
  { key: "settings", label: "Settings and data" },
  { key: "cache", label: "Cache" },
  { key: "logs", label: "Logs" },
];

/** Where Seedbox 2 keeps things on this computer, each with a Show in Finder/Explorer/file manager button. */
function FoldersPanel() {
  const system = useApp((s) => s.system);
  return (
    <Panel className="max-w-3xl">
      <PanelHeader title="Folders" description="Where Seedbox 2 keeps its files on this computer" />
      {FOLDERS.map(({ key, label }) => {
        const path = system?.folders[key];
        return (
          <InfoRow key={key} label={label}>
            <span className="min-w-0 flex-1 break-all font-mono text-xs">{path ?? <Pending width="w-72" />}</span>
            {path && system && <IconButton icon={FolderOpen} label={system.reveal_label} tooltip={system.reveal_label} size="sm" onClick={() => void api.reveal(path)} />}
          </InfoRow>
        );
      })}
    </Panel>
  );
}

export function SystemPage() {
  const system = useApp((s) => s.system);
  const connection = useApp((s) => s.connection);
  return (
    <Page>
      <PageHeader title="Settings" subtitle="What the Seedbox 2 engine is running on, how waveforms look, and where it keeps its files" />
      <PageBody className="flex flex-col gap-4">
        <Panel className="max-w-3xl">
          <PanelHeader title="Engine" />
          <InfoRow label="Connection">
            <StatusDot tone={connection === "live" ? "ok" : connection === "offline" ? "danger" : "warn"} label={connection} />
          </InfoRow>
          <InfoRow label="Version">{system?.version ?? <Pending width="w-20" />}</InfoRow>
          <InfoRow label="Platform">{system ? `${system.platform} (${system.machine})` : <Pending width="w-28" />}</InfoRow>
          <InfoRow label="Python">{system?.python ?? <Pending width="w-14" />}</InfoRow>
          <InfoRow label="ffmpeg">
            <Value v={system?.ffmpeg} />
          </InfoRow>
          <InfoRow label="deno">
            <Value v={system?.deno} />
          </InfoRow>
          <InfoRow label="Frontend">
            <Value v={system ? (system.frontend ?? "Vite dev server") : undefined} />
          </InfoRow>
        </Panel>
        <WatchFoldersPanel />
        <ModelsPanel />
        <StemsPanel />
        <MasterPanel />
        <PowerPanel />
        <AssistantPanel />
        <WaveSettingsPanel className="max-w-3xl" />
        <FoldersPanel />
      </PageBody>
    </Page>
  );
}
