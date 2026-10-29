import { Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { comboLabel } from "@/lib/hotkeys";
import { Link, useRoute } from "@/lib/router";
import { Logo } from "./Logo";
import { type NavEntry, RAIL, SETTINGS_NAV } from "./nav";

const PITCH = 44; // item height (h-10) + gap (gap-1), for the sliding indicator
const INSET = 8; // (h-10 − h-6) / 2

function RailItem({ entry, active }: { entry: NavEntry; active: boolean }) {
  const { icon: Icon, label, combo } = entry;
  return (
    <Tooltip side="right" delay={200} content={<span className="flex items-center gap-2">{label}<span className="text-fg-subtle">{comboLabel(combo)}</span></span>}>
      <Link
        to={{ name: entry.route }}
        aria-label={label}
        aria-current={active ? "page" : undefined}
        className={cn(
          "group grid size-10 place-items-center rounded-md transition-colors",
          active ? "bg-raised text-accent-hover" : "text-fg-subtle hover:bg-raised/60 hover:text-fg",
        )}
      >
        <Icon size={18} strokeWidth={active ? 2.2 : 1.9} />
      </Link>
    </Tooltip>
  );
}

/** The slim icon rail: the logo (home), the areas, and Settings at the bottom. The accent bar slides to the active area. */
export function Rail() {
  const route = useRoute();
  const index = RAIL.findIndex((e) => e.route === route.name);
  return (
    <aside className="flex w-rail shrink-0 flex-col items-center border-r border-line bg-panel">
      <Tooltip side="right" content="Home">
        <Link to={{ name: "home" }} aria-label="Home" className="grid h-11 w-rail shrink-0 place-items-center border-b border-line">
          <Logo />
        </Link>
      </Tooltip>
      <nav aria-label="Areas" className="relative flex w-full flex-1 flex-col items-center gap-1 pt-2">
        <span
          aria-hidden
          className={cn("absolute top-2 left-0 h-6 w-0.75 rounded-r-full bg-accent transition-[transform,opacity] duration-slow ease-spring", index < 0 && "opacity-0")}
          style={{ transform: `translateY(${Math.max(index, 0) * PITCH + INSET}px)` }}
        />
        {RAIL.map((e) => (
          <RailItem key={e.route} entry={e} active={route.name === e.route} />
        ))}
      </nav>
      <div className="relative flex w-full justify-center border-t border-line py-2">
        {route.name === "settings" && <span aria-hidden className="absolute top-4 left-0 h-6 w-0.75 animate-fade-in rounded-r-full bg-accent" />}
        <RailItem entry={SETTINGS_NAV} active={route.name === "settings"} />
      </div>
    </aside>
  );
}
