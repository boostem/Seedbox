// #/downloads/<job_id> — one job's live per-track progress (store kept live by SSE), cancel, open the crate.
import { ArrowLeft, ArrowRight, CircleAlert, FolderOpen, Loader, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { Job, JobItem } from "@/api/types";
import { usePageTitle } from "@/components/shell/pageTitle";
import {
  Badge,
  Button,
  CoverArt,
  EmptyState,
  Page,
  PageHeader,
  ProgressBar,
  Spinner,
  StatusDot,
  Table,
  TableScroll,
  TBody,
  Td,
  Th,
  THead,
  Tr,
} from "@/components/ui";
import { fmtDateTime, tildify } from "@/lib/format";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { countsSummary, isActiveItem, isLive, JOB_STATE_LABEL, JOB_STATE_TONE, jobProgress, STATUS_TONE } from "./status";

export function JobView({ jobId }: { jobId: string }) {
  const job = useApp((s) => s.jobs[jobId]);
  const epoch = useApp((s) => s.epoch);
  const [missing, setMissing] = useState(false);
  usePageTitle("Downloads", job?.folder);

  // Fetch on open and after every SSE reconnect; the store merges without rolling back live progress.
  useEffect(() => {
    let alive = true;
    void useApp
      .getState()
      .refreshJob(jobId)
      .then((j) => {
        if (alive && !j && !useApp.getState().jobs[jobId]) setMissing(true);
      });
    return () => {
      alive = false;
    };
  }, [jobId, epoch]);

  if (!job) {
    return missing ? (
      <EmptyState
        icon={CircleAlert}
        title="Job not found"
        description="The engine only remembers jobs of its current session. The files it downloaded are still in their crate."
        action={
          <Button icon={ArrowLeft} onClick={() => navigate({ name: "downloads", jobId: "" })}>
            All downloads
          </Button>
        }
      />
    ) : (
      <div className="grid h-full place-items-center">
        <Spinner />
      </div>
    );
  }
  return <JobDetail job={job} />;
}

function JobDetail({ job }: { job: Job }) {
  const { done, total, counts } = jobProgress(job);
  const live = isLive(job);
  const [cancelling, setCancelling] = useState(false);

  const cancel = async () => {
    setCancelling(true);
    try {
      useApp.getState().upsertJob(await api.cancelJob(job.job_id));
    } catch {
      // the client already toasted the error
    } finally {
      setCancelling(false);
    }
  };

  const summary = countsSummary(counts);
  return (
    <Page>
      <PageHeader
        title={job.folder}
        subtitle={
          <span className="inline-flex items-center gap-2">
            <StatusDot tone={JOB_STATE_TONE[job.state]} pulse={live} label={JOB_STATE_LABEL[job.state]} />
            <span className="text-fg-subtle">·</span>
            <span className="num">
              {done}/{total}
            </span>
            {summary && (
              <>
                <span className="text-fg-subtle">·</span>
                <span>{summary}</span>
              </>
            )}
            <span className="text-fg-subtle">·</span>
            <span title={job.finished_at ? `finished ${fmtDateTime(job.finished_at)}` : undefined}>started {fmtDateTime(job.started_at)}</span>
          </span>
        }
        actions={
          <>
            <Button variant="ghost" icon={ArrowLeft} onClick={() => navigate({ name: "downloads", jobId: "" })}>
              All downloads
            </Button>
            {live ? (
              <Button variant="danger" icon={X} loading={cancelling} disabled={job.state === "finalizing"} onClick={cancel}>
                Cancel job
              </Button>
            ) : (
              <Button variant="primary" iconRight={ArrowRight} onClick={() => navigate({ name: "set", folder: job.folder })}>
                Open crate
              </Button>
            )}
          </>
        }
      >
        <ProgressBar
          className="mb-3"
          size="md"
          value={job.state === "finalizing" ? null : total ? done / total : 1}
          tone={job.state === "cancelled" ? "warn" : job.state === "done" ? "ok" : "accent"}
          label="Job progress"
        />
      </PageHeader>
      <TableScroll className="flex-1">
        <Table>
          <THead>
            <Tr interactive={false}>
              <Th width={44} />
              <Th>Track</Th>
              <Th width={170}>Status</Th>
              <Th width={140}>Genre</Th>
              <Th width={320}>File / reason</Th>
            </Tr>
          </THead>
          <TBody>
            {job.items.map((it) => (
              <ItemRow key={it.track.id} item={it} />
            ))}
          </TBody>
        </Table>
      </TableScroll>
    </Page>
  );
}

function ItemRow({ item }: { item: JobItem }) {
  const t = item.track;
  const active = isActiveItem(item);
  return (
    <Tr rowHeight={44} interactive={false} dim={item.status === "skipped" || item.status === "exists"}>
      <Td>
        <CoverArt src={t.cover_url} size={28} camelot={t.camelot} alt="" />
      </Td>
      <Td truncate>
        <div className="truncate font-medium">{t.name}</div>
        <div className="truncate text-xs text-fg-muted">{t.artists.join(", ")}</div>
      </Td>
      <Td>
        {item.status ? (
          <Badge tone={STATUS_TONE[item.status]}>{item.status}</Badge>
        ) : (
          <span className={active ? "inline-flex items-center gap-1.5 text-sm text-accent-hover" : "text-sm text-fg-subtle"}>
            {active && <Loader size={13} className="animate-spin" />}
            {item.text}
          </span>
        )}
      </Td>
      <Td truncate muted className="text-xs">
        {item.genre ?? ""}
      </Td>
      <Td truncate className="text-xs">
        {item.path ? (
          <button
            type="button"
            className="inline-flex max-w-full items-center gap-1.5 text-fg-muted hover:text-fg"
            title={`Reveal ${item.path}`}
            onClick={() => void api.reveal(item.path!)}
          >
            <FolderOpen size={13} className="shrink-0" />
            <span className="truncate">{tildify(item.path)}</span>
          </button>
        ) : item.reason ? (
          <span className={item.status === "failed" ? "text-danger" : "text-fg-muted"} title={item.reason}>
            {item.reason}
          </span>
        ) : null}
      </Td>
    </Tr>
  );
}
