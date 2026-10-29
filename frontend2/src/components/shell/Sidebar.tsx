import { cn } from "@/lib/cn";
import { comboLabel } from "@/lib/hotkeys";
import { Link, useRoute } from "@/lib/router";
import { useApp } from "@/store/app";
import { Logo } from "./Logo";
import { NAV, type NavEntry, SYSTEM_NAV } from "./nav";

function NavItem({ entry, active }: { entry: NavEntry; active: boolean }) {
  const { icon: Icon, label, combo } = entry;
  return (
    <Link
      to={{ name: entry.route }}
      className={cn(
        "group relative flex h-8 items-center gap-2.5 rounded-sm px-2.5 text-base font-medium transition-colors",
        active ? "bg-raised text-fg" : "text-fg-muted hover:bg-raised/60 hover:text-fg",
      )}
    >
      {active && <span className="absolute top-1.5 bottom-1.5 left-0 w-[2px] rounded-full bg-accent" />}
      <Icon size={16} strokeWidth={active ? 2.2 : 1.9} className={active ? "text-accent-hover" : "text-fg-subtle group-hover:text-fg-muted"} />
      <span className="flex-1 truncate">{label}</span>
      {combo && <span className="text-2xs text-fg-faint opacity-0 transition-opacity group-hover:opacity-100">{comboLabel(combo)}</span>}
    </Link>
  );
}

export function Sidebar() {
  const route = useRoute();
  const version = useApp((s) => s.system?.version);
  return (
    <aside className="flex w-[212px] shrink-0 flex-col border-r border-line bg-panel">
      <div className="flex h-11 shrink-0 items-center gap-2.5 border-b border-line px-3.5">
        <Logo />
        <span className="text-md font-semibold tracking-tight">Seedbox 2</span>
        {version && <span className="mt-0.5 truncate text-2xs text-fg-faint num">v{version}</span>}
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 px-2 pt-3">
        {NAV.map((e) => (
          <NavItem key={e.route} entry={e} active={route.name === e.route} />
        ))}
      </nav>
      <div className="border-t border-line px-2 py-2">
        <NavItem entry={SYSTEM_NAV} active={route.name === "system"} />
      </div>
    </aside>
  );
}
