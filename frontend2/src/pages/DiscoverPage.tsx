import { AlertTriangle, ArrowLeft, Boxes, Check, Download, RotateCw, Search, SearchX, X } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { TunebatCrate, TunebatCrateJob, TunebatCrateTrack, TunebatTrack } from "@/api/types";
import { Badge, Button, Checkbox, CoverArt, EmptyState, Input, KeyBadge, Page, PageBody, PageHeader, Panel, PanelHeader, ProgressBar, ScoreChip, Skeleton, Spinner, Switch } from "@/components/ui";
import { fmtTime } from "@/lib/format";
import { navigate, useHashQuery } from "@/lib/router";

const TICK_ALL_KEY = "seedbox2.tunebat.tickAll";

type Search = { state: "idle" } | { state: "searching" } | { state: "done"; term: string; items: TunebatTrack[] } | { state: "failed"; error: string };
type Opened = { state: "loading"; seed: TunebatTrack } | { state: "open"; crate: TunebatCrate } | { state: "failed"; seed: TunebatTrack; error: string };

const message = (err: unknown) => (err instanceof ApiError ? err.message : String(err));

function loadTickAll(): boolean {
  try {
    return localStorage.getItem(TICK_ALL_KEY) === "1";
  } catch {
    return false;
  }
}

/** What starts ticked: the seed and the related recommendations (all of them with `tickAll`), not what the library has. */
export function defaultTicks(crate: TunebatCrate, tickAll: boolean): Set<string> {
  const ids = crate.recs.filter((r) => !r.in_library && (tickAll || r.related)).map((r) => r.track.id);
  return new Set(crate.seed.in_library ? ids : [crate.seed.id, ...ids]);
}

const FINISHED: TunebatCrateJob["state"][] = ["done", "cancelled", "failed"];
const STATUS_WORD: Record<string, string> = { downloaded: "Downloaded", copied: "Copied", exists: "Already there", skipped: "Not found", failed: "Failed" };

function ItemStatus({ item }: { item: TunebatCrateJob["items"][number] | undefined }) {
  if (!item) return null;
  if (item.status) {
    const ok = item.status !== "failed" && item.status !== "skipped";
    return (
      <span className={ok ? "flex items-center gap-1 text-xs text-ok" : "text-xs text-danger"} title={item.reason ?? undefined}>
        {ok && <Check size={13} />}
        {STATUS_WORD[item.status] ?? item.status}
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-xs text-fg-muted">
      <Spinner size={12} /> {item.text}
    </span>
  );
}

function TrackCells({ track, size = 28 }: { track: TunebatCrateTrack | TunebatTrack; size?: number }) {
  return (
    <>
      <CoverArt src={track.cover_url} size={size} camelot={track.camelot || undefined} rounded="xs" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{track.name}</div>
        <div className="truncate text-xs text-fg-subtle">{track.artists.join(", ")}</div>
      </div>
      <span className="num w-9 text-right text-xs text-fg-muted">{track.bpm ? Math.round(track.bpm) : "–"}</span>
      <span className="w-9">{track.camelot && <KeyBadge camelot={track.camelot} size="xs" />}</span>
      <span className="num w-10 text-right text-xs text-fg-subtle">{fmtTime(track.duration)}</span>
    </>
  );
}

function JobBar({ job, onCancel }: { job: TunebatCrateJob; onCancel: () => void }) {
  const finished = job.items.filter((i) => i.status).length;
  const got = job.items.filter((i) => i.status && i.status !== "failed" && i.status !== "skipped").length;
  const running = !FINISHED.includes(job.state);
  return (
    <div className="flex flex-col gap-2 border-b border-line px-4 py-3" role="status" aria-label="Download">
      <div className="flex items-center gap-3 text-sm">
        {running ? <Spinner size={14} /> : job.state === "done" ? <Check size={14} className="text-ok" /> : <AlertTriangle size={14} className="text-danger" />}
        <span className="flex-1">
          {job.state === "queued" && "Waiting for the download before it…"}
          {job.state === "running" && `Downloading ${finished} of ${job.items.length}…`}
          {job.state === "finishing" && "Writing the crate and importing it…"}
          {job.state === "done" && `${got} of ${job.items.length} in the crate “${job.folder}”. They show up in the library as they're imported and analysed.`}
          {job.state === "cancelled" && `Stopped: ${got} of ${job.items.length} in the crate “${job.folder}”.`}
          {job.state === "failed" && <span className="text-danger">{job.error}</span>}
        </span>
        {running && (
          <Button size="sm" icon={X} onClick={onCancel}>
            Stop
          </Button>
        )}
        {job.state === "done" && (
          <Button size="sm" icon={Boxes} onClick={() => navigate({ name: "crates" })}>
            Crates
          </Button>
        )}
      </div>
      {running && <ProgressBar value={job.state === "running" ? finished / Math.max(1, job.items.length) : null} />}
    </div>
  );
}

function CrateView({ crate, onBack }: { crate: TunebatCrate; onBack: () => void }) {
  const [tickAll, setTickAllState] = useState(loadTickAll);
  const [ticks, setTicks] = useState<Set<string>>(() => defaultTicks(crate, tickAll));
  const [job, setJob] = useState<TunebatCrateJob | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(
    () =>
      onServerEvent((ev) => {
        if (ev.type !== "tunebat.crate") return;
        const j = ev.data as TunebatCrateJob;
        setJob((cur) => (cur && cur.id === j.id ? j : cur));
      }),
    [],
  );

  const setTickAll = (on: boolean) => {
    setTickAllState(on);
    try {
      localStorage.setItem(TICK_ALL_KEY, on ? "1" : "0");
    } catch {
      /* private mode: just this time */
    }
    setTicks(defaultTicks(crate, on));
  };
  const toggle = (id: string, on: boolean) =>
    setTicks((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const related = crate.recs.filter((r) => r.related).length;
  const items = useMemo(() => new Map((job?.items ?? []).map((i) => [i.id, i])), [job]);
  const running = job != null && !FINISHED.includes(job.state);

  const start = async () => {
    setStarting(true);
    try {
      setJob(await api.tunebat.downloadCrate(crate.seed.id, [...ticks], { quiet: true }));
    } catch (err) {
      setJob({ id: "", seed_id: crate.seed.id, seed: crate.folder, folder: crate.folder, state: "failed", error: message(err), items: [] });
    } finally {
      setStarting(false);
    }
  };
  const cancel = () => job && void api.tunebat.cancelCrateDownload(job.id, { quiet: true }).catch(() => undefined);

  return (
    <Panel>
      <PanelHeader
        title={crate.folder}
        description={`${crate.recs.length} recommendations, ${related} mix with it (a compatible key and the tempo within ±${Math.round(crate.tolerance * 100)} %)`}
        actions={
          <Button size="sm" variant="ghost" icon={ArrowLeft} onClick={onBack}>
            Results
          </Button>
        }
      />
      {job && <JobBar job={job} onCancel={cancel} />}
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2">
        <Switch size="sm" checked={tickAll} onChange={setTickAll} label="Tick every recommendation" />
        <span className="flex-1" />
        <Button size="sm" variant="ghost" onClick={() => setTicks(defaultTicks(crate, false))}>
          Related only
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setTicks(new Set([crate.seed.id, ...crate.recs.map((r) => r.track.id)]))}>
          All
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setTicks(new Set())}>
          None
        </Button>
        <Button size="sm" variant="primary" icon={Download} disabled={!ticks.size || running || starting} onClick={() => void start()}>
          Download {ticks.size} {ticks.size === 1 ? "track" : "tracks"}
        </Button>
      </div>
      <ul className="flex flex-col px-4 py-1" aria-label="Crate">
        <li className="flex items-center gap-2.5 border-b border-line py-2">
          <Checkbox checked={ticks.has(crate.seed.id)} onChange={(on) => toggle(crate.seed.id, on)} title={`Tick ${crate.seed.name}`} />
          <TrackCells track={crate.seed} size={36} />
          <span className="flex w-40 justify-end gap-1.5">
            <Badge tone="accent">Seed</Badge>
            {crate.seed.in_library && <Badge>In library</Badge>}
          </span>
          <span className="flex w-28 justify-end">
            <ItemStatus item={items.get(crate.seed.id)} />
          </span>
        </li>
        {crate.recs.map((r) => (
          <li key={r.track.id} className={r.related ? "flex items-center gap-2.5 border-b border-line/60 py-1.5 last:border-b-0" : "flex items-center gap-2.5 border-b border-line/60 py-1.5 opacity-70 last:border-b-0"}>
            <Checkbox checked={ticks.has(r.track.id)} onChange={(on) => toggle(r.track.id, on)} title={`Tick ${r.track.name}`} />
            <TrackCells track={r.track} />
            <span className="flex w-40 items-center justify-end gap-1.5" title={r.reason}>
              {r.in_library && <Badge>In library</Badge>}
              {r.related ? <Badge tone="ok">Mixes</Badge> : <Badge variant="outline">Other</Badge>}
              <ScoreChip value={r.score} move={r.move} passed={r.related} />
            </span>
            <span className="flex w-28 justify-end">
              <ItemStatus item={items.get(r.track.id)} />
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

/** Seedbox 1's tunebat flow (`#/discover?q=…` starts with a search): search a song, open it with every track tunebat recommends from it, tick, download as a crate. */
export function DiscoverPage() {
  const query = useHashQuery();
  const initial = query.get("q") ?? "";
  const [term, setTerm] = useState(initial);
  const [search, setSearch] = useState<Search>({ state: "idle" });
  const [opened, setOpened] = useState<Opened | null>(null);

  const run = async (e?: FormEvent, what = term) => {
    e?.preventDefault();
    const q = what.trim();
    if (!q) return;
    setOpened(null);
    setSearch({ state: "searching" });
    try {
      const { items } = await api.tunebat.search(q, { quiet: true });
      setSearch({ state: "done", term: q, items });
    } catch (err) {
      setSearch({ state: "failed", error: message(err) });
    }
  };
  // #/discover?q=… searches straight away
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => void (initial.trim() && run(undefined, initial)), []);
  const open = async (seed: TunebatTrack) => {
    setOpened({ state: "loading", seed });
    try {
      const crate = await api.tunebat.crate(seed.id, { quiet: true });
      setOpened({ state: "open", crate });
    } catch (err) {
      setOpened({ state: "failed", seed, error: message(err) });
    }
  };

  return (
    <Page>
      <PageHeader title="Discover" subtitle="Search tunebat for a song, then download it with the tracks that mix with it, as a crate." />
      <PageBody className="flex flex-col gap-4">
        <form className="flex items-center gap-2" onSubmit={(e) => void run(e)}>
          <Input wrapperClassName="flex-1 max-w-2xl" autoFocus icon={Search} placeholder="A song, an artist, or a Spotify link" value={term} onChange={(e) => setTerm(e.target.value)} aria-label="Search tunebat" />
          <Button type="submit" variant="primary" disabled={!term.trim() || search.state === "searching"}>
            Search
          </Button>
        </form>

        {opened?.state === "open" && <CrateView key={opened.crate.seed.id} crate={opened.crate} onBack={() => setOpened(null)} />}
        {opened?.state === "loading" && (
          <Panel role="status" aria-label="Opening" className="flex flex-col gap-2 p-4">
            <div className="flex items-center gap-2 text-sm text-fg-muted">
              <Spinner size={14} /> Asking tunebat what goes with {opened.seed.name}…
            </div>
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-7 w-full" />
            ))}
          </Panel>
        )}
        {opened?.state === "failed" && (
          <Panel className="flex items-center gap-2 p-4 text-sm text-danger" role="alert">
            <AlertTriangle size={14} className="shrink-0" />
            <span className="flex-1">{opened.error}</span>
            <Button size="sm" icon={RotateCw} onClick={() => void open(opened.seed)}>
              Retry
            </Button>
          </Panel>
        )}

        {!opened && search.state === "searching" && (
          <Panel role="status" aria-label="Searching" className="flex flex-col gap-2 p-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-7 w-full" />
            ))}
          </Panel>
        )}
        {!opened && search.state === "failed" && (
          <Panel className="flex items-center gap-2 p-4 text-sm text-danger" role="alert">
            <AlertTriangle size={14} className="shrink-0" />
            <span className="flex-1">{search.error}</span>
            <Button size="sm" icon={RotateCw} onClick={() => void run()}>
              Retry
            </Button>
          </Panel>
        )}
        {!opened && search.state === "done" && !search.items.length && <EmptyState icon={SearchX} title={`Nothing found for “${search.term}”`} description="Try an artist's name." />}
        {!opened && search.state === "done" && search.items.length > 0 && (
          <Panel>
            <PanelHeader title="Results" description="Open one to see what tunebat recommends with it." />
            <ul className="flex flex-col px-4 py-1" aria-label="Results">
              {search.items.map((t) => (
                <li key={t.id} className="flex items-center gap-2.5 border-b border-line/60 py-1.5 last:border-b-0">
                  <TrackCells track={t} />
                  <Button size="sm" onClick={() => void open(t)}>
                    Open
                  </Button>
                </li>
              ))}
            </ul>
          </Panel>
        )}
        {!opened && search.state === "idle" && (
          <EmptyState icon={Search} title="Find a song to build a crate around" description="Its recommendations that mix with it (key and tempo) start ticked; the others are there to add." />
        )}
      </PageBody>
    </Page>
  );
}
