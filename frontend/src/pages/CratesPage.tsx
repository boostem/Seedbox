import { ArrowRight, FolderOpen, LayoutGrid, Library, List, RefreshCw, Search, Sprout } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { CrateSummary } from "@/api/types";
import { usePageTitle } from "@/components/shell/pageTitle";
import {
  Button,
  CoverArt,
  EmptyState,
  IconButton,
  Input,
  KeyBadge,
  nextSort,
  Page,
  PageBody,
  PageHeader,
  Select,
  Spinner,
  Table,
  TableScroll,
  Tabs,
  TBody,
  Td,
  Th,
  THead,
  Tr,
} from "@/components/ui";
import { parseCamelot } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtDateTime, fmtRelative, plural, tildify } from "@/lib/format";
import { useHotkey } from "@/lib/hotkeys";
import { navigate, useHashQuery } from "@/lib/router";
import { useApp } from "@/store/app";

type View = "grid" | "list";
type SortKey = "updated" | "name" | "tracks" | "key" | "bpm";
const VIEW_KEY = "seedbox.crates.view";

function loadView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "grid";
  } catch {
    return "grid";
  }
}

function title(c: CrateSummary): string {
  return c.seed?.name ?? c.folder;
}

function keyOrder(code: string | undefined): number {
  const p = parseCamelot(code);
  return p ? p.n * 2 + (p.letter === "B" ? 1 : 0) : 99;
}

function compare(a: CrateSummary, b: CrateSummary, key: SortKey): number {
  switch (key) {
    case "updated":
      return a.updated_at.localeCompare(b.updated_at);
    case "name":
      return title(a).localeCompare(title(b), undefined, { sensitivity: "base" });
    case "tracks":
      return a.track_count - b.track_count;
    case "key":
      return keyOrder(a.seed?.camelot) - keyOrder(b.seed?.camelot);
    case "bpm":
      return (a.seed?.bpm ?? 0) - (b.seed?.bpm ?? 0);
  }
}

const open = (c: CrateSummary) => navigate({ name: "set", folder: c.folder });
const reveal = (c: CrateSummary) => void api.reveal(c.path);

function CrateCard({ crate }: { crate: CrateSummary }) {
  const s = crate.seed;
  return (
    <div
      role="link"
      tabIndex={0}
      onClick={() => open(crate)}
      onKeyDown={(e) => e.key === "Enter" && open(crate)}
      className="group flex min-w-0 flex-col overflow-hidden rounded-md border border-line bg-panel shadow-card transition-colors hover:border-line-strong hover:bg-raised/40"
    >
      <div className="relative">
        <CoverArt src={s?.cover_url} fill rounded="xs" camelot={s?.camelot} alt={title(crate)} className="rounded-none" />
        <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-black/0 to-black/0 opacity-0 transition-opacity group-hover:opacity-100" />
        <div className="absolute inset-x-2 bottom-2 flex items-center gap-1.5 opacity-0 transition-opacity group-hover:opacity-100">
          <Button size="xs" variant="secondary" iconRight={ArrowRight} onClick={(e) => (e.stopPropagation(), open(crate))}>
            Open set
          </Button>
          <span className="flex-1" />
          <IconButton
            icon={FolderOpen}
            label="Reveal in Finder"
            size="sm"
            variant="secondary"
            onClick={(e) => {
              e.stopPropagation();
              reveal(crate);
            }}
          />
          {s && (
            <IconButton
              icon={Sprout}
              label="More recommendations for this seed"
              size="sm"
              variant="secondary"
              onClick={(e) => {
                e.stopPropagation();
                navigate({ name: "crate", seedId: s.id });
              }}
            />
          )}
        </div>
        <span className="absolute top-2 right-2 rounded-xs bg-black/65 px-1.5 py-0.5 text-2xs font-semibold text-white backdrop-blur-sm num">{crate.track_count}</span>
      </div>
      <div className="flex flex-col gap-1 px-2.5 pt-2 pb-2.5">
        <div className="truncate text-sm font-semibold" title={crate.folder}>
          {title(crate)}
        </div>
        <div className="truncate text-xs text-fg-muted">{s ? s.artists.join(", ") : <span className="text-fg-subtle italic">No crate metadata</span>}</div>
        <div className="mt-1 flex items-center gap-2 text-2xs text-fg-subtle">
          {s && <KeyBadge camelot={s.camelot} keyName={s.key} size="xs" />}
          {s && <span className="text-fg-muted num">{fmtBpm(s.bpm)} BPM</span>}
          <span className="flex-1" />
          <span title={fmtDateTime(crate.updated_at)} className="num">
            {fmtRelative(crate.updated_at)}
          </span>
        </div>
      </div>
    </div>
  );
}

function CrateTable({ crates, sort, onSort }: { crates: CrateSummary[]; sort: { key: SortKey; dir: "asc" | "desc" }; onSort: (k: SortKey) => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const dir = (k: SortKey) => (sort.key === k ? sort.dir : null);
  return (
    <TableScroll className="h-full">
      <Table>
        <THead>
          <Tr interactive={false}>
            <Th width={60} />
            <Th sort={dir("name")} onSort={() => onSort("name")}>
              Seed
            </Th>
            <Th width={64} sort={dir("key")} onSort={() => onSort("key")}>
              Key
            </Th>
            <Th width={64} align="right" sort={dir("bpm")} onSort={() => onSort("bpm")}>
              BPM
            </Th>
            <Th width={72} align="right" sort={dir("tracks")} onSort={() => onSort("tracks")}>
              Tracks
            </Th>
            <Th width={120} sort={dir("updated")} onSort={() => onSort("updated")}>
              Updated
            </Th>
            <Th>Folder</Th>
            <Th width={96} />
          </Tr>
        </THead>
        <TBody>
          {crates.map((c) => (
            <Tr key={c.folder} selected={selected === c.folder} onClick={() => setSelected(c.folder)} onDoubleClick={() => open(c)} rowHeight={46}>
              <Td className="pr-0 pl-5">
                <CoverArt src={c.seed?.cover_url} size={32} rounded="xs" camelot={c.seed?.camelot} />
              </Td>
              <Td truncate>
                <div className="truncate font-medium text-fg">{title(c)}</div>
                <div className="truncate text-xs text-fg-muted">{c.seed ? c.seed.artists.join(", ") : "No crate metadata"}</div>
              </Td>
              <Td>{c.seed ? <KeyBadge camelot={c.seed.camelot} keyName={c.seed.key} /> : <span className="text-fg-faint">—</span>}</Td>
              <Td align="right" num>
                {fmtBpm(c.seed?.bpm)}
              </Td>
              <Td align="right" num>
                {c.track_count}
              </Td>
              <Td muted num title={fmtDateTime(c.updated_at)}>
                {fmtRelative(c.updated_at)}
              </Td>
              <Td truncate className="font-mono text-xs text-fg-subtle" title={c.path}>
                {tildify(c.path)}
              </Td>
              <Td align="right" className="pr-4">
                <span className="inline-flex items-center gap-0.5 opacity-60 group-hover/row:opacity-100">
                  {c.seed && <IconButton icon={Sprout} label="More recommendations for this seed" size="sm" onClick={() => navigate({ name: "crate", seedId: c.seed!.id })} />}
                  <IconButton icon={FolderOpen} label="Reveal in Finder" size="sm" onClick={() => reveal(c)} />
                  <IconButton icon={ArrowRight} label="Open set builder" size="sm" onClick={() => open(c)} />
                </span>
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </TableScroll>
  );
}

export function CratesPage() {
  usePageTitle("Crates");
  const crates = useApp((s) => s.crates);
  const root = useApp((s) => s.settings?.output_root ?? s.system?.output_root);
  const epoch = useApp((s) => s.epoch);
  const initialView = useHashQuery().get("view");
  const [view, setView] = useState<View>(() => (initialView === "list" || initialView === "grid" ? initialView : loadView()));
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "updated", dir: "desc" });
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    setRefreshing(true);
    await useApp.getState().refreshCrates();
    setRefreshing(false);
  };
  useEffect(() => {
    void useApp.getState().refreshCrates();
  }, [epoch]);
  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch {
      /* ignore */
    }
  }, [view]);
  useHotkey("Mod+f", () => document.getElementById("crate-filter")?.focus());

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const list = (crates ?? []).filter(
      (c) => !q || c.folder.toLowerCase().includes(q) || c.seed?.artists.some((a) => a.toLowerCase().includes(q)) || c.seed?.camelot.toLowerCase() === q,
    );
    const sign = sort.dir === "desc" ? -1 : 1;
    return list.sort((a, b) => sign * compare(a, b, sort.key));
  }, [crates, filter, sort]);

  const totalTracks = (crates ?? []).reduce((n, c) => n + c.track_count, 0);

  return (
    <Page>
      <PageHeader
        title="Crates"
        subtitle={
          crates ? (
            <span className="num">
              {plural(crates.length, "crate")} · {plural(totalTracks, "track")}
              {root && <span className="text-fg-subtle"> · {tildify(root)}</span>}
            </span>
          ) : (
            "Loading…"
          )
        }
        actions={
          <>
            <IconButton icon={RefreshCw} label="Refresh" loading={refreshing} onClick={() => void refresh()} />
            {root && (
              <Button icon={FolderOpen} onClick={() => void api.reveal(root)}>
                Reveal folder
              </Button>
            )}
            <Button icon={Search} variant="primary" onClick={() => navigate({ name: "search" })}>
              New crate
            </Button>
          </>
        }
      >
        <div className="flex h-11 items-center gap-2">
          <Input
            id="crate-filter"
            icon={Search}
            placeholder="Filter by seed, artist or key…"
            wrapperClassName="w-72"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && (setFilter(""), e.currentTarget.blur())}
            suffix={filter ? `${shown.length}` : undefined}
          />
          <span className="flex-1" />
          {view === "grid" && (
            <label className="flex items-center gap-2 text-xs text-fg-subtle">
              Sort
              <Select value={`${sort.key}:${sort.dir}`} onChange={(e) => {
                const [key, dir] = e.target.value.split(":") as [SortKey, "asc" | "desc"];
                setSort({ key, dir });
              }}>
                <option value="updated:desc">Recently updated</option>
                <option value="name:asc">Name A–Z</option>
                <option value="tracks:desc">Most tracks</option>
                <option value="key:asc">Camelot key</option>
                <option value="bpm:asc">BPM</option>
              </Select>
            </label>
          )}
          <Tabs
            variant="segmented"
            value={view}
            onChange={setView}
            items={[
              { id: "grid", label: "Grid", icon: LayoutGrid },
              { id: "list", label: "List", icon: List },
            ]}
          />
        </div>
      </PageHeader>

      {crates === null ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-sm text-fg-subtle">
          <Spinner /> Loading crates…
        </div>
      ) : crates.length === 0 ? (
        <EmptyState
          className="flex-1"
          icon={Library}
          title="No crates yet"
          description="Search for a seed track, tick the recommendations you like and download them — each seed becomes a crate here."
          action={
            <Button variant="primary" icon={Search} onClick={() => navigate({ name: "search" })}>
              Search for a seed
            </Button>
          }
        />
      ) : shown.length === 0 ? (
        <EmptyState className="flex-1" icon={Search} title="No matching crates" description={`Nothing matches “${filter}”.`} action={<Button onClick={() => setFilter("")}>Clear filter</Button>} />
      ) : view === "grid" ? (
        <PageBody className="px-5 py-5">
          <div className="grid grid-cols-[repeat(auto-fill,minmax(176px,1fr))] gap-4">
            {shown.map((c) => (
              <CrateCard key={c.folder} crate={c} />
            ))}
          </div>
        </PageBody>
      ) : (
        <div className={cn("min-h-0 flex-1")}>
          <CrateTable crates={shown} sort={sort} onSort={(k) => setSort((s) => nextSort(s, k, k === "name" || k === "key" ? "asc" : "desc"))} />
        </div>
      )}
    </Page>
  );
}

