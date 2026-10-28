// #/downloads — every job of this engine session (GET /api/jobs, kept live by SSE), newest first.
import { Download, Search } from "lucide-react";
import { useEffect, useMemo } from "react";
import { api } from "@/api/client";
import type { Job } from "@/api/types";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Badge, Button, EmptyState, Page, PageHeader, ProgressBar, StatusDot, Table, TableScroll, TBody, Td, Th, THead, Tr } from "@/components/ui";
import { fmtDateTime, fmtRelative, plural } from "@/lib/format";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { countsSummary, isLive, JOB_STATE_LABEL, JOB_STATE_TONE, jobProgress } from "./status";

export function JobList() {
  usePageTitle("Downloads");
  const jobsById = useApp((s) => s.jobs);
  const epoch = useApp((s) => s.epoch);
  const jobs = useMemo(() => Object.values(jobsById).sort((a, b) => b.started_at.localeCompare(a.started_at)), [jobsById]);
  const live = jobs.filter(isLive).length;

  // Reload the history on open and after every SSE reconnect (events may have been missed meanwhile).
  useEffect(() => {
    const ctl = new AbortController();
    api
      .jobs({ quiet: true, signal: ctl.signal })
      .then((list) => {
        for (const j of list) void useApp.getState().refreshJob(j.job_id);
      })
      .catch(() => {});
    return () => ctl.abort();
  }, [epoch]);

  return (
    <Page>
      <PageHeader
        title="Downloads"
        subtitle={jobs.length ? `${plural(jobs.length, "job")} this session${live ? ` · ${live} running` : ""}` : "Download jobs of this session"}
      />
      {jobs.length ? (
        <TableScroll className="flex-1">
          <Table>
            <THead>
              <Tr interactive={false}>
                <Th>Crate</Th>
                <Th width={160}>State</Th>
                <Th width={220}>Progress</Th>
                <Th width={260}>Result</Th>
                <Th width={150} align="right">
                  Started
                </Th>
              </Tr>
            </THead>
            <TBody>
              {jobs.map((job) => (
                <JobRow key={job.job_id} job={job} />
              ))}
            </TBody>
          </Table>
        </TableScroll>
      ) : (
        <EmptyState
          icon={Download}
          title="No downloads yet"
          description="Open a crate, tick the tracks you want and press Download. Jobs show up here with live progress."
          action={
            <Button variant="primary" icon={Search} onClick={() => navigate({ name: "search" })}>
              Find a seed
            </Button>
          }
        />
      )}
    </Page>
  );
}

function JobRow({ job }: { job: Job }) {
  const { done, total, counts } = jobProgress(job);
  const live = isLive(job);
  const open = () => navigate({ name: "downloads", jobId: job.job_id });
  return (
    <Tr rowHeight={44} onClick={open} className="cursor-pointer">
      <Td truncate className="font-medium">
        {job.folder}
      </Td>
      <Td>
        <StatusDot tone={JOB_STATE_TONE[job.state]} pulse={live} label={<span className="text-sm">{JOB_STATE_LABEL[job.state]}</span>} />
      </Td>
      <Td>
        <div className="flex items-center gap-2">
          <ProgressBar
            className="flex-1"
            value={job.state === "finalizing" ? null : total ? done / total : 1}
            tone={job.state === "cancelled" ? "warn" : job.state === "done" ? "ok" : "accent"}
            label={`${done} of ${total}`}
          />
          <span className="num w-12 shrink-0 text-right text-xs text-fg-muted">
            {done}/{total}
          </span>
        </div>
      </Td>
      <Td truncate muted className="text-xs">
        {counts.failed ? (
          <Badge tone="danger" className="mr-1.5">
            {counts.failed} failed
          </Badge>
        ) : null}
        {countsSummary({ ...counts, failed: undefined }) || (live ? "…" : "—")}
      </Td>
      <Td align="right" muted className="text-xs" title={fmtDateTime(job.started_at)}>
        {fmtRelative(job.started_at)}
      </Td>
    </Tr>
  );
}
