/** Settings → Report a problem (#89): a zip of the logs, system info, the self-test and the analysis versions (no
 * audio), written into the logs folder, shown in Finder/Explorer and ready to attach to a new GitHub issue. */
import { ExternalLink, FileArchive, FolderOpen } from "lucide-react";
import { useState } from "react";
import { api } from "@/api/client";
import type { ProblemReport } from "@/api/types";
import { Button, IconButton, InfoRow, Panel, PanelHeader, StatusDot } from "@/components/ui";
import { fmtBytes, tildify } from "@/lib/format";
import { useApp } from "@/store/app";

export function ReportPanel() {
  const revealLabel = useApp((s) => s.system?.reveal_label ?? "Show");
  const [report, setReport] = useState<ProblemReport | null>(null);
  const [busy, setBusy] = useState(false);

  const make = async () => {
    setBusy(true);
    try {
      const r = await api.report();
      setReport(r);
      void api.reveal(r.path, { quiet: true });
    } catch {
      /* the client toasted the error */
    }
    setBusy(false);
  };

  return (
    <Panel className="max-w-3xl">
      <PanelHeader
        title="Report a problem"
        description="A zip of the logs, this computer's details, a self-test and the analysis versions. No audio, no library, no settings."
        actions={
          <Button size="xs" icon={FileArchive} loading={busy} onClick={() => void make()}>
            {busy ? "Running the self-test…" : "Create report"}
          </Button>
        }
      />
      {report && (
        <>
          <InfoRow label="Report">
            <span className="min-w-0 flex-1 break-all font-mono text-xs">{tildify(report.path)}</span>
            <span className="shrink-0 text-xs text-fg-subtle">{fmtBytes(report.size)}</span>
            <IconButton icon={FolderOpen} label={revealLabel} tooltip={revealLabel} size="sm" onClick={() => void api.reveal(report.path)} />
          </InfoRow>
          <InfoRow label="Self-test">
            <StatusDot tone={report.self_test_ok ? "ok" : report.self_test_ok === false ? "danger" : "warn"} label={report.self_test_ok ? "Passed" : report.self_test_ok === false ? "Failed (the report says why)" : "Didn't run"} />
          </InfoRow>
          <InfoRow label="Next">
            <span className="flex-1 text-fg-muted">Open an issue, say what happened and attach the zip.</span>
            <a className="inline-flex shrink-0 items-center gap-1 text-xs text-accent-hover hover:underline" href={report.issue_url} target="_blank" rel="noreferrer">
              New issue on GitHub <ExternalLink className="size-3" />
            </a>
          </InfoRow>
        </>
      )}
    </Panel>
  );
}
