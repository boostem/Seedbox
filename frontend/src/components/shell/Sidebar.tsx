import { Download, Library, ListMusic, type LucideIcon, Search, Settings } from "lucide-react";
import type { ReactNode } from "react";
import { CoverArt, KeyBadge, Spinner } from "@/components/ui";
import { APP_NAME, isSetbox } from "@/lib/appMode";
import { cn } from "@/lib/cn";
import { comboLabel } from "@/lib/hotkeys";
import { href, Link, type Route, useRoute } from "@/lib/router";
import { useActiveJobs, useApp } from "@/store/app";
import { Logo } from "./Logo";

function NavItem({ to, icon: Icon, label, active, combo, trailing }: { to: Route; icon: LucideIcon; label: string; active: boolean; combo?: string; trailing?: ReactNode }) {
  return (
    <Link
      to={to}
      className={cn(
        "group relative flex h-8 items-center gap-2.5 rounded-sm px-2.5 text-base font-medium transition-colors",
        active ? "bg-raised text-fg" : "text-fg-muted hover:bg-raised/60 hover:text-fg",
      )}
    >
      {active && <span className="absolute top-1.5 bottom-1.5 left-0 w-[2px] rounded-full bg-accent" />}
      <Icon size={16} strokeWidth={active ? 2.2 : 1.9} className={active ? "text-accent-hover" : "text-fg-subtle group-hover:text-fg-muted"} />
      <span className="flex-1 truncate">{label}</span>
      {trailing}
      {combo && !trailing && <span className="text-2xs text-fg-faint opacity-0 transition-opacity group-hover:opacity-100">{comboLabel(combo)}</span>}
    </Link>
  );
}

export function Sidebar() {
  const route = useRoute();
  const crates = useApp((s) => s.crates);
  const active = useActiveJobs();
  const version = useApp((s) => s.system?.version);
  const recent = [...(crates ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 8);
  const done = active.reduce((n, j) => n + j.items.filter((i) => i.status).length, 0);
  const total = active.reduce((n, j) => n + j.items.length, 0);

  return (
    <aside className="flex w-[212px] shrink-0 flex-col border-r border-line bg-panel">
      <div className="flex h-11 shrink-0 items-center gap-2.5 border-b border-line px-3.5">
        <Logo />
        <span className="text-[15px] font-semibold tracking-tight">{APP_NAME}</span>
        {version && <span className="mt-0.5 text-2xs text-fg-faint num">v{version}</span>}
      </div>
      {isSetbox ? (
        <nav className="flex flex-col gap-0.5 px-2 pt-3">
          <NavItem to={{ name: "crates" }} icon={Library} label="Crates" combo="Mod+1" active={route.name === "crates" || route.name === "set"} />
          <NavItem to={{ name: "setlists" }} icon={ListMusic} label="Setlists" combo="Mod+2" active={route.name === "setlists" || route.name === "setlist"} />
        </nav>
      ) : (
        <nav className="flex flex-col gap-0.5 px-2 pt-3">
          <NavItem to={{ name: "search" }} icon={Search} label="Search" combo="Mod+1" active={route.name === "search" || route.name === "crate"} />
          <NavItem to={{ name: "crates" }} icon={Library} label="Crates" combo="Mod+2" active={route.name === "crates"} />
          <NavItem
            to={active.length === 1 ? { name: "downloads", jobId: active[0].job_id } : { name: "downloads", jobId: "" }}
            icon={Download}
            label="Downloads"
            combo="Mod+3"
            active={route.name === "downloads"}
            trailing={
              active.length ? (
                <span className="inline-flex items-center gap-1 text-2xs text-accent-hover num">
                  <Spinner size={10} />
                  {done}/{total}
                </span>
              ) : undefined
            }
          />
        </nav>
      )}

      <div className="mt-5 flex min-h-0 flex-1 flex-col">
        <div className="label-caps flex items-center justify-between px-4 pb-1.5">
          <span>Recent crates</span>
          {crates && <span className="text-fg-faint num">{crates.length}</span>}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 scroll-thin">
          {recent.map((c) => {
            // Setbox opens the crate's set; Seedbox its seed's recommendations (crates without a seed: the list)
            const to: Route = isSetbox ? { name: "set", folder: c.folder } : c.seed ? { name: "crate", seedId: c.seed.id } : { name: "crates" };
            const isActive = isSetbox ? route.name === "set" && route.folder === c.folder : route.name === "crate" && route.seedId === c.seed?.id;
            return (
              <a
                key={c.folder}
                href={href(to)}
                draggable={false}
                title={c.folder}
                className={cn(
                  "flex h-9 items-center gap-2 rounded-sm px-1.5 transition-colors",
                  isActive ? "bg-raised text-fg" : "text-fg-muted hover:bg-raised/60 hover:text-fg",
                )}
              >
                <CoverArt src={c.seed?.cover_url} size={24} rounded="xs" camelot={c.seed?.camelot} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm leading-tight">{c.seed?.name ?? c.folder}</div>
                  <div className="truncate text-2xs leading-tight text-fg-subtle">{c.seed ? c.seed.artists.join(", ") : `${c.track_count} tracks`}</div>
                </div>
                {c.seed && <KeyBadge camelot={c.seed.camelot} size="xs" variant="text" />}
              </a>
            );
          })}
          {crates && crates.length === 0 && (
            <p className="px-2 py-1 text-xs text-fg-faint">{isSetbox ? "No crates yet: download some with Seedbox." : "No crates yet: search for a seed track."}</p>
          )}
        </div>
      </div>

      <div className="border-t border-line px-2 py-2">
        <NavItem to={{ name: "settings" }} icon={Settings} label="Settings" combo="Mod+," active={route.name === "settings"} />
      </div>
    </aside>
  );
}
