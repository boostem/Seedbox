import { ChevronLeft, ChevronRight } from "lucide-react";
import { IconButton, StatusDot, Tooltip } from "@/components/ui";
import { useRoute } from "@/lib/router";
import { type ConnectionState, useApp } from "@/store/app";
import { TITLES } from "./nav";

const CONNECTION: Record<ConnectionState, { tone: "ok" | "warn" | "danger" | "neutral"; label: string; help: string }> = {
  connecting: { tone: "neutral", label: "Connecting", help: "Connecting to the Seedbox 2 engine…" },
  live: { tone: "ok", label: "Live", help: "Connected: live updates on" },
  reconnecting: { tone: "warn", label: "Reconnecting", help: "Lost the event stream, retrying" },
  offline: { tone: "danger", label: "Offline", help: "The Seedbox 2 engine isn't responding" },
};

/** Back/forward, the page title and the engine connection. */
export function TopBar() {
  const route = useRoute();
  const connection = useApp((s) => s.connection);
  const c = CONNECTION[connection];
  return (
    <header className="flex h-11 shrink-0 items-center gap-2 border-b border-line bg-panel px-3">
      <IconButton icon={ChevronLeft} label="Back" tooltip="Back (⌘[)" size="sm" onClick={() => history.back()} />
      <IconButton icon={ChevronRight} label="Forward" tooltip="Forward (⌘])" size="sm" onClick={() => history.forward()} />
      <h2 className="ml-1 truncate text-base font-semibold">{TITLES[route.name]}</h2>
      <div className="flex-1" />
      <Tooltip content={c.help}>
        <span className="text-xs text-fg-muted">
          <StatusDot tone={c.tone} pulse={connection === "connecting" || connection === "reconnecting"} label={c.label} />
        </span>
      </Tooltip>
    </header>
  );
}
