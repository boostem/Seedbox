import { ChevronLeft, ChevronRight, Keyboard, Search } from "lucide-react";
import { IconButton, Kbd, StatusDot, Tooltip } from "@/components/ui";
import { AnalysisQueue } from "@/features/library/AnalysisQueue";
import { comboLabel } from "@/lib/hotkeys";
import { useRoute } from "@/lib/router";
import { type ConnectionState, useApp } from "@/store/app";
import { ui } from "@/store/ui";
import { TITLES } from "./nav";

const CONNECTION: Record<ConnectionState, { tone: "ok" | "warn" | "danger" | "neutral"; label: string; help: string }> = {
  connecting: { tone: "neutral", label: "Connecting", help: "Connecting to the Seedbox 2 engine…" },
  live: { tone: "ok", label: "Live", help: "Connected: live updates on" },
  reconnecting: { tone: "warn", label: "Reconnecting", help: "Lost the event stream, retrying" },
  offline: { tone: "danger", label: "Offline", help: "The Seedbox 2 engine isn't responding" },
};

/** Where you are (back/forward, the area), the way to everything else (⌘K) and the engine connection. */
export function TopBar() {
  const route = useRoute();
  const connection = useApp((s) => s.connection);
  const c = CONNECTION[connection];
  return (
    <header className="grid h-11 shrink-0 grid-cols-[1fr_auto_1fr] items-center gap-3 border-b border-line bg-panel px-3">
      <div className="flex min-w-0 items-center gap-1">
        <IconButton icon={ChevronLeft} label="Back" tooltip={`Back (${comboLabel("Mod+[")})`} size="sm" onClick={() => history.back()} />
        <IconButton icon={ChevronRight} label="Forward" tooltip={`Forward (${comboLabel("Mod+]")})`} size="sm" onClick={() => history.forward()} />
        <h2 className="ml-1 truncate text-base font-semibold">{TITLES[route.name]}</h2>
      </div>
      <button
        type="button"
        onClick={ui.openPalette}
        className="flex h-control-md w-96 max-w-full items-center gap-2 rounded-sm border border-line bg-raised/60 px-2.5 text-sm text-fg-subtle transition-colors hover:border-line-strong hover:text-fg-muted"
      >
        <Search size={14} />
        <span className="flex-1 truncate text-left">Go to or run anything…</span>
        <Kbd combo="Mod+K" subtle />
      </button>
      <div className="flex items-center justify-end gap-2">
        <AnalysisQueue />
        <Tooltip content={c.help}>
          <span className="text-xs text-fg-muted">
            <StatusDot tone={c.tone} pulse={connection === "connecting" || connection === "reconnecting"} label={c.label} />
          </span>
        </Tooltip>
        <IconButton icon={Keyboard} label="Keyboard shortcuts" tooltip="Keyboard shortcuts (?)" size="sm" onClick={ui.openShortcuts} />
      </div>
    </header>
  );
}
