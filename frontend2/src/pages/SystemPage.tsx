import { InfoRow, Page, PageBody, PageHeader, Panel, PanelHeader, StatusDot } from "@/components/ui";
import { useApp } from "@/store/app";

function Value({ v }: { v: string | null | undefined }) {
  return v ? <span className="truncate font-mono text-xs">{v}</span> : <span className="text-fg-faint">not found</span>;
}

export function SystemPage() {
  const system = useApp((s) => s.system);
  const connection = useApp((s) => s.connection);
  return (
    <Page>
      <PageHeader title="System" subtitle="What the Seedbox 2 engine is running on" />
      <PageBody>
        <Panel className="max-w-[720px]">
          <PanelHeader title="Engine" />
          <InfoRow label="Connection">
            <StatusDot tone={connection === "live" ? "ok" : connection === "offline" ? "danger" : "warn"} label={connection} />
          </InfoRow>
          <InfoRow label="Version">{system?.version ?? "…"}</InfoRow>
          <InfoRow label="Platform">{system ? `${system.platform} (${system.machine})` : "…"}</InfoRow>
          <InfoRow label="Python">{system?.python ?? "…"}</InfoRow>
          <InfoRow label="ffmpeg">
            <Value v={system?.ffmpeg} />
          </InfoRow>
          <InfoRow label="deno">
            <Value v={system?.deno} />
          </InfoRow>
          <InfoRow label="Frontend">
            <Value v={system?.frontend ?? (system ? "Vite dev server" : null)} />
          </InfoRow>
        </Panel>
      </PageBody>
    </Page>
  );
}
