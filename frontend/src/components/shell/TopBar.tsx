import { ChevronLeft, ChevronRight, CircleArrowUp } from "lucide-react";
import { Button, IconButton, Spinner, StatusDot, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { href, type Route, useRoute } from "@/lib/router";
import { type ConnectionState, useActiveJobs, useApp } from "@/store/app";
import { runYtdlpUpdate, useYtdlpPendingRestart, useYtdlpUpdate, ytdlpOutdated } from "@/store/system";
import { usePageTitleStore } from "./pageTitle";

const TITLES: Record<Route["name"], string> = {
  search: "Search",
  crate: "Crate",
  downloads: "Downloads",
  crates: "Crates",
  set: "Set builder",
  setlists: "Setlists",
  setlist: "Setlist",
  settings: "Settings",
  ui: "Design system",
  notFound: "Not found",
};

const CONNECTION: Record<ConnectionState, { tone: "ok" | "warn" | "danger" | "neutral"; label: string; help: string }> = {
  connecting: { tone: "neutral", label: "Connecting", help: "Connecting to the Seedbox engine…" },
  live: { tone: "ok", label: "Live", help: "Connected — live updates on" },
  reconnecting: { tone: "warn", label: "Reconnecting", help: "Lost the event stream — retrying" },
  offline: { tone: "danger", label: "Offline", help: "The Seedbox engine isn't responding" },
};

function PreviewActivity() {
  const counts = useApp((s) => {
    let busy = 0;
    let ready = 0;
    for (const p of Object.values(s.previews)) {
      if (p.state === "queued" || p.state === "loading") busy++;
      else if (p.state === "ready") ready++;
    }
    return `${busy}/${ready}`;
  });
  const [busy, ready] = counts.split("/").map(Number);
  if (!busy) return null;
  return (
    <Tooltip content={`${ready} preview clips ready, ${busy} loading in the background`}>
      <span className="inline-flex items-center gap-1.5 text-xs text-fg-muted num">
        <Spinner size={11} className="text-info" />
        Previews {ready}/{ready + busy}
      </span>
    </Tooltip>
  );
}

function JobActivity() {
  const jobs = useActiveJobs();
  if (!jobs.length) return null;
  const job = jobs[0];
  const done = job.items.filter((i) => i.status).length;
  return (
    <a href={href({ name: "downloads", jobId: job.job_id })} className="inline-flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-xs text-fg-muted hover:bg-raised hover:text-fg num">
      <Spinner size={11} className="text-accent-hover" />
      {job.state === "finalizing" ? "Finalizing crate…" : `Downloading ${done}/${job.items.length}`}
    </a>
  );
}

function YtdlpStatus() {
  const system = useApp((s) => s.system);
  const running = useYtdlpUpdate((s) => s.running);
  const pending = useYtdlpPendingRestart(system?.ytdlp_version);
  if (!system) return null;
  const outdated = ytdlpOutdated(system.ytdlp_version, system.ytdlp_latest);
  if (pending && !running) {
    return (
      <Tooltip content={`yt-dlp ${pending} is installed and will be used after you restart Seedbox`}>
        <span className="inline-flex items-center gap-1.5 text-xs text-fg-subtle num">
          <span className="size-1.5 rounded-full bg-ok" />
          yt-dlp <span className="text-fg-muted">{system.ytdlp_version}</span> → <span className="text-ok">{pending}</span>
          <span className="text-fg-subtle">· restart to apply</span>
        </span>
      </Tooltip>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <Tooltip content={outdated ? `yt-dlp ${system.ytdlp_latest} is available — YouTube changes often, update if downloads fail` : "yt-dlp (YouTube downloader) version"}>
        <span className="inline-flex items-center gap-1.5 text-xs text-fg-subtle num">
          {outdated && <span className="size-1.5 rounded-full bg-warn" />}
          yt-dlp <span className="text-fg-muted">{system.ytdlp_version}</span>
        </span>
      </Tooltip>
      {(outdated || running) && (
        <Button size="xs" variant="secondary" icon={CircleArrowUp} loading={running} onClick={() => void runYtdlpUpdate()} className="border-warn/40 text-warn hover:text-warn">
          {running ? "Updating…" : "Update"}
        </Button>
      )}
    </span>
  );
}

export function TopBar() {
  const route = useRoute();
  const parts = usePageTitleStore((s) => s.parts);
  const connection = useApp((s) => s.connection);
  const mock = useApp((s) => s.mock);
  const crumbs = parts ?? [TITLES[route.name]];
  const conn = CONNECTION[connection];

  return (
    <header className="flex h-11 shrink-0 items-center gap-3 border-b border-line bg-panel pr-3 pl-2">
      <div className="flex items-center">
        <IconButton icon={ChevronLeft} label="Back" tooltip="Back (⌘[)" size="sm" onClick={() => history.back()} />
        <IconButton icon={ChevronRight} label="Forward" tooltip="Forward (⌘])" size="sm" onClick={() => history.forward()} />
      </div>
      <nav className="flex min-w-0 flex-1 items-center gap-1.5 text-sm">
        {crumbs.map((c, i) => (
          <span key={i} className="flex min-w-0 items-center gap-1.5">
            {i > 0 && <span className="text-fg-faint">/</span>}
            <span className={cn("truncate", i === crumbs.length - 1 ? "font-medium text-fg" : "text-fg-subtle")}>{c}</span>
          </span>
        ))}
      </nav>
      <div className="flex shrink-0 items-center gap-4">
        <JobActivity />
        <PreviewActivity />
        <YtdlpStatus />
        <span className="h-4 w-px bg-line-strong" />
        {mock ? (
          <Tooltip content="VITE_MOCK=1 — fixtures + simulated events, no backend">
            <StatusDot tone="boost" label={<span className="text-xs font-medium text-move-boost">Mock data</span>} />
          </Tooltip>
        ) : (
          <Tooltip content={conn.help}>
            <StatusDot tone={conn.tone} pulse={connection !== "live"} label={<span className="text-xs text-fg-muted">{conn.label}</span>} />
          </Tooltip>
        )}
      </div>
    </header>
  );
}
