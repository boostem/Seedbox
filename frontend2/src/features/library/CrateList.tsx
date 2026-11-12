/**
 * The library's sources (#73): all tracks, smart crates (saved filters), crates and sets. Crates and sets take
 * tracks dragged from the table (TRACKS_MIME); dropping on "New crate" or "New set" makes one with them. A set shows
 * that it won't take tracks that aren't ready to mix (#100): the page then says why.
 */
import { FolderPlus, Library, ListMusic, ListPlus, Sparkles, X } from "lucide-react";
import { type DragEvent, type ReactNode, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { TRACKS_MIME, UNREADY_ALL_MIME, UNREADY_SOME_MIME } from "./LibraryTable";

export type Scope = { kind: "all" } | { kind: "crate"; id: number } | { kind: "set"; id: number } | { kind: "smart"; id: string };

export function sameScope(a: Scope, b: Scope): boolean {
  return a.kind === b.kind && (a.kind === "all" || (a as { id: unknown }).id === (b as { id: unknown }).id);
}

export interface CrateListProps {
  scope: Scope;
  onScope: (s: Scope) => void;
  total: number | null;
  crates: { id: number; name: string; track_count: number }[];
  sets: { id: number; name: string; track_count: number }[];
  smartCrates: { id: string; name: string }[];
  onDeleteSmart: (id: string) => void;
  /** Tracks dropped on a crate or set (or on "new", with `id` null). */
  onDrop: (target: "crate" | "set", id: number | null, hashes: string[]) => void;
}

/** The hashes a drag from the table carries, or null for anything else (files from the OS, text). */
export function draggedTracks(dt: DataTransfer): string[] | null {
  const raw = dt.getData(TRACKS_MIME);
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as unknown;
    return Array.isArray(v) && v.every((h) => typeof h === "string") ? v : null;
  } catch {
    return null;
  }
}

const byName = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

export function CrateList({ scope, onScope, total, crates: unsortedCrates, sets: unsortedSets, smartCrates, onDeleteSmart, onDrop }: CrateListProps) {
  // by name, so a crate doesn't jump to the top when tracks are dropped on it
  const crates = useMemo(() => [...unsortedCrates].sort((a, b) => byName.compare(a.name, b.name)), [unsortedCrates]);
  const sets = useMemo(() => [...unsortedSets].sort((a, b) => byName.compare(a.name, b.name)), [unsortedSets]);
  return (
    <nav className="flex w-56 shrink-0 flex-col gap-4 overflow-y-auto border-r border-line bg-panel px-2 py-3 scroll-thin" aria-label="Crates and sets">
      <Item icon={Library} active={scope.kind === "all"} onClick={() => onScope({ kind: "all" })} count={total}>
        All tracks
      </Item>
      {smartCrates.length > 0 && (
        <Group title="Smart crates">
          {smartCrates.map((c) => (
            <Item
              key={c.id}
              icon={Sparkles}
              active={scope.kind === "smart" && scope.id === c.id}
              onClick={() => onScope({ kind: "smart", id: c.id })}
              aside={
                <X
                  size={12}
                  className="opacity-0 group-hover/item:opacity-60 hover:!opacity-100"
                  aria-label={`Delete ${c.name}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDeleteSmart(c.id);
                  }}
                />
              }
            >
              {c.name}
            </Item>
          ))}
        </Group>
      )}
      <Group title="Crates">
        {crates.map((c) => (
          <Item key={c.id} icon={ListMusic} active={scope.kind === "crate" && scope.id === c.id} onClick={() => onScope({ kind: "crate", id: c.id })} count={c.track_count} onTracks={(h) => onDrop("crate", c.id, h)}>
            {c.name}
          </Item>
        ))}
        <Item icon={FolderPlus} muted onTracks={(h) => onDrop("crate", null, h)} title="Drop tracks here to make a crate of them">
          New crate
        </Item>
      </Group>
      <Group title="Sets">
        {sets.map((s) => (
          <Item key={s.id} icon={ListMusic} active={scope.kind === "set" && scope.id === s.id} onClick={() => onScope({ kind: "set", id: s.id })} count={s.track_count} onTracks={(h) => onDrop("set", s.id, h)} forSets>
            {s.name}
          </Item>
        ))}
        <Item icon={ListPlus} muted onTracks={(h) => onDrop("set", null, h)} forSets title="Drop tracks here to start a set with them">
          New set
        </Item>
      </Group>
    </nav>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="px-2 pb-1 text-2xs font-semibold tracking-caps text-fg-subtle uppercase">{title}</div>
      {children}
    </div>
  );
}

interface ItemProps {
  icon: typeof Library;
  children: ReactNode;
  active?: boolean;
  muted?: boolean;
  count?: number | null;
  aside?: ReactNode;
  title?: string;
  onClick?: () => void;
  /** Makes it a drop target for tracks dragged from the table. */
  onTracks?: (hashes: string[]) => void;
  /** A set (or "New set"): it takes only tracks that are ready to mix. */
  forSets?: boolean;
}

function Item({ icon: Icon, children, active, muted, count, aside, title, onClick, onTracks, forSets }: ItemProps) {
  const [over, setOver] = useState<false | "ok" | "some" | "none">(false);
  const accepts = (e: DragEvent) => !!onTracks && Array.from(e.dataTransfer.types).includes(TRACKS_MIME);
  const fit = (e: DragEvent) => {
    const types = Array.from(e.dataTransfer.types);
    return !forSets ? "ok" : types.includes(UNREADY_ALL_MIME) ? "none" : types.includes(UNREADY_SOME_MIME) ? "some" : "ok";
  };
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      onDragEnter={(e) => accepts(e) && setOver(fit(e))}
      onDragOver={(e) => {
        if (!accepts(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        setOver(false);
        const hashes = accepts(e) ? draggedTracks(e.dataTransfer) : null;
        if (!hashes) return;
        e.preventDefault();
        e.stopPropagation();
        onTracks?.(hashes);
      }}
      className={cn(
        "group/item flex h-7 w-full items-center gap-2 rounded-sm px-2 text-left text-sm transition-colors",
        active ? "bg-selected text-fg" : muted ? "text-fg-subtle hover:bg-hover hover:text-fg-muted" : "text-fg-muted hover:bg-hover hover:text-fg",
        over === "ok" && "bg-accent-soft text-fg ring-1 ring-accent/70",
        over === "some" && "bg-warn/10 text-fg ring-1 ring-warn/60",
        over === "none" && "cursor-not-allowed bg-danger/10 text-fg ring-1 ring-danger/60",
      )}
    >
      <Icon size={14} className="shrink-0" />
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {aside}
      {over === "none" || over === "some" ? (
        <span className="text-2xs whitespace-nowrap text-fg-muted">{over === "none" ? "Not ready" : "Ready ones only"}</span>
      ) : (
        count !== undefined && count !== null && <span className="num text-xs text-fg-faint">{count}</span>
      )}
    </button>
  );
}
