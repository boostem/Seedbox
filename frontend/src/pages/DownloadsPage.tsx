// OWNER: FRONTEND-CRATE agent. #/downloads/<job_id> = one job's live progress; #/downloads/all = the job list.
import { JobList } from "@/features/downloads/JobList";
import { JobView } from "@/features/downloads/JobView";

/** Job ids the list answers to (the router maps a bare #/downloads to the crates page for now). */
const LIST_IDS = new Set(["", "all", "recent", "jobs"]);

export function DownloadsPage({ jobId }: { jobId: string }) {
  return LIST_IDS.has(jobId) ? <JobList /> : <JobView jobId={jobId} />;
}
