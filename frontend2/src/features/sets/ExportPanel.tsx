/**
 * Export the set as one continuous mix (#70): a format, a bar with what it's doing and the time left, cancel, and where the
 * files went with what they measured. The work is the server's `set_export` job, so it goes on if this view closes, and
 * the view picks it up again: `job.*` events, and `GET /sets/{id}/export` when it opens.
 */
import { Download, FolderOpen, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { JobRow, SetExportResult, SetExportStatus } from "@/api/types";
import { Button, Panel, PanelHeader, ProgressBar, Select } from "@/components/ui";
import { toast } from "@/store/toasts";

const live = (job: JobRow | null) => job?.state === "queued" || job?.state === "running";

function fmtLength(seconds: number): string {
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor(total / 60) % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}` : `${m}:${String(total % 60).padStart(2, "0")}`;
}

export function ExportPanel({ setId }: { setId: number }) {
  const [status, setStatus] = useState<SetExportStatus | null>(null);
  const [format, setFormat] = useState("mp3");
  const [job, setJob] = useState<JobRow | null>(null);
  const [progress, setProgress] = useState<{ fraction: number; message: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const found = await api.sets.exportStatus(setId);
    setStatus(found);
    setJob(found.job);
    return found;
  }, [setId]);

  useEffect(() => {
    setJob(null);
    setProgress(null);
    refresh().catch(() => setStatus(null));
  }, [refresh]);

  const jobId = job?.id;
  useEffect(
    () =>
      onServerEvent((ev) => {
        if (!ev.type.startsWith("job.")) return;
        const data = ev.data as { id?: string; kind?: string; progress?: number; message?: string | null };
        if (data.kind !== "set_export" || data.id !== jobId) return;
        if (ev.type === "job.progress") setProgress({ fraction: data.progress ?? 0, message: data.message ?? null });
        else if (ev.type !== "job.queued" && ev.type !== "job.started") {
          setProgress(null);
          refresh().catch(() => undefined);
        }
      }),
    [jobId, refresh],
  );

  const start = async () => {
    setBusy(true);
    try {
      setJob(await api.sets.export(setId, format));
      setProgress(null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      setJob(await api.jobs.cancel(job.id));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  if (!status) return null;
  const running = live(job);
  const result = job?.state === "done" ? (job.result as SetExportResult | null) : null;
  return (
    <Panel>
      <PanelHeader
        title="Export the mix"
        description={`The whole set as one continuous mix, with a tracklist and a cue sheet. It goes to ${status.folder}.`}
        actions={
          running ? (
            <Button size="sm" icon={X} onClick={cancel}>
              Cancel
            </Button>
          ) : (
            <div className="flex items-center gap-2">
              <Select aria-label="Format" value={format} onChange={(e) => setFormat(e.target.value)}>
                {status.formats.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.label}
                  </option>
                ))}
              </Select>
              <Button size="sm" variant="primary" icon={Download} onClick={start} loading={busy}>
                Export
              </Button>
            </div>
          )
        }
      />
      {running && (
        <div className="flex flex-col gap-1.5 p-4">
          <ProgressBar value={progress?.fraction ?? job?.progress ?? 0} label="Export progress" />
          <p className="text-xs text-fg-subtle">{progress?.message ?? (job?.state === "queued" ? "Waiting for its turn" : "Starting")}</p>
        </div>
      )}
      {job?.state === "failed" && (
        <p role="alert" className="p-4 text-xs text-danger">
          The export failed: {job.error}. Exporting again picks up from the last finished track.
        </p>
      )}
      {result && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 p-4 text-xs text-fg-muted">
          <span className="min-w-0 truncate text-fg">{result.path}</span>
          <span>{fmtLength(result.duration_s)}</span>
          <span>{result.measured.integrated_lufs.toFixed(1)} LUFS</span>
          {result.measured.lra_lu !== null && <span>LRA {result.measured.lra_lu.toFixed(1)} LU</span>}
          <span>{result.measured.true_peak_dbtp.toFixed(1)} dBTP</span>
          <Button size="sm" icon={FolderOpen} onClick={() => void api.reveal(result.path)}>
            Show the file
          </Button>
        </div>
      )}
    </Panel>
  );
}
