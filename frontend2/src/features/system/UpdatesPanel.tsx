/** Settings → Updates (#89): Seedbox 2's own version (checked on GitHub, downloaded and verified in the background,
 * installed on restart) and yt-dlp's, which goes stale within weeks and updates on its own from PyPI. */
import { ExternalLink, RefreshCw, RotateCcw, RotateCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { UpdatesStatus } from "@/api/types";
import { Button, InfoRow, Panel, PanelHeader, ProgressBar, Skeleton, StatusDot, Switch } from "@/components/ui";
import { toast } from "@/store/toasts";
import { appUpdateLine } from "./updates";

const POLL_MS = 1000;

export function UpdatesPanel() {
  const [status, setStatus] = useState<UpdatesStatus | null>(null);
  const [busy, setBusy] = useState<"ytdlp" | "rollback" | "install" | null>(null);
  const refresh = useCallback(() => api.updates.status({ quiet: true }).then(setStatus, () => undefined), []);

  useEffect(() => void refresh(), [refresh]);
  const active = !!status && (status.app.state === "checking" || status.app.state === "downloading" || status.ytdlp.checking || status.ytdlp.updating);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(id);
  }, [active, refresh]);

  const app = status?.app;
  const yt = status?.ytdlp;
  const line = app ? appUpdateLine(app) : null;

  const updateYtdlp = async () => {
    setBusy("ytdlp");
    try {
      const r = await api.updates.updateYtdlp();
      toast.success(r.message, { title: "yt-dlp" });
    } catch {
      /* the client toasted the error */
    }
    setBusy(null);
    void refresh();
  };
  const rollback = async () => {
    setBusy("rollback");
    try {
      const r = await api.updates.rollbackYtdlp();
      toast.info(r.message, { title: "yt-dlp" });
    } catch {
      /* toasted */
    }
    setBusy(null);
    void refresh();
  };
  const install = async () => {
    setBusy("install");
    try {
      await api.updates.install();
      toast.info("Seedbox 2 closes, installs the update and opens again.", { title: "Updating" });
    } catch {
      setBusy(null);
    }
  };

  return (
    <Panel className="max-w-3xl">
      <PanelHeader
        title="Updates"
        description="New versions of Seedbox 2 download in the background and install when you restart"
        actions={
          <Button size="xs" variant="ghost" icon={RefreshCw} loading={app?.state === "checking"} disabled={!status} onClick={() => void api.updates.check().then(setStatus, () => undefined)}>
            Check now
          </Button>
        }
      />
      {!app || !yt || !line ? (
        <InfoRow label="Seedbox 2">
          <Skeleton className="w-60" />
        </InfoRow>
      ) : (
        <>
          <InfoRow label="Seedbox 2">
            <span className="font-mono text-xs">{app.current}</span>
          </InfoRow>
          <InfoRow label="Check at startup">
            <Switch checked={app.enabled} disabled={!app.installable} onChange={(on) => void api.updates.setCheck(on).then(setStatus, () => undefined)} label={app.installable ? undefined : <span className="text-fg-faint">{app.reason}</span>} />
          </InfoRow>
          <div className="flex flex-col gap-2 border-b border-line/60 px-4 py-2.5">
            <div className="flex items-center gap-2 text-sm">
              <StatusDot tone={line.tone} pulse={app.state === "checking" || app.state === "downloading"} />
              <span className="min-w-0 flex-1">{line.text}</span>
              {app.latest?.url && (
                <a className="inline-flex shrink-0 items-center gap-1 text-xs text-accent-hover hover:underline" href={app.latest.url} target="_blank" rel="noreferrer">
                  What's new <ExternalLink className="size-3" />
                </a>
              )}
              {line.canInstall && (
                <Button size="xs" variant="primary" icon={RotateCw} loading={busy === "install"} onClick={() => void install()}>
                  Restart to update
                </Button>
              )}
            </div>
            {app.state === "downloading" && <ProgressBar value={line.progress} size="xs" label="Download" />}
          </div>
          <InfoRow label="yt-dlp">
            <span className="font-mono text-xs">{yt.running}</span>
            <span className="flex-1 text-xs text-fg-subtle">
              {yt.restart
                ? `${yt.installed} is installed and used after a restart`
                : yt.update_available
                  ? `${yt.latest} is out`
                  : yt.latest
                    ? "the newest"
                    : yt.error
                      ? `couldn't check (${yt.error})`
                      : yt.checking
                        ? "checking…"
                        : ""}
            </span>
            {yt.can_roll_back && (
              <Button size="xs" variant="ghost" icon={RotateCcw} loading={busy === "rollback"} disabled={busy !== null} onClick={() => void rollback()}>
                Roll back
              </Button>
            )}
            <Button size="xs" variant={yt.update_available ? "primary" : "secondary"} loading={busy === "ytdlp" || yt.updating} disabled={busy !== null} onClick={() => void updateYtdlp()}>
              Update yt-dlp
            </Button>
          </InfoRow>
        </>
      )}
    </Panel>
  );
}
