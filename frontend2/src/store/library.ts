/**
 * The library view's data (#73): every track's row (GET /api/library/view), the crates and sets to drop tracks
 * into and the smart crates. Rows follow the job queue live: a job starting or queueing about a track marks its row
 * at once, a finished one re-reads that row (in batches) and drops its cached overview if it was the waveform.
 */
import { create } from "zustand";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { CrateSummary, LibraryRow, SetSummary } from "@/api/types";
import { cleanFilter, type SmartCrate } from "@/features/library/filter";
import { overviews } from "@/features/library/overviews";
import { readiness } from "@/features/library/readiness";
import { useImports } from "@/store/imports";

interface LibraryState {
  rows: LibraryRow[] | null;
  crates: CrateSummary[];
  sets: SetSummary[];
  smartCrates: SmartCrate[];
  error: string | null;
}

export const useLibrary = create<LibraryState>()(() => ({ rows: null, crates: [], sets: [], smartCrates: [], error: null }));

export async function loadLibrary(): Promise<void> {
  try {
    const { items } = await api.library.view(undefined, { quiet: true });
    useLibrary.setState({ rows: items, error: null });
  } catch (e) {
    useLibrary.setState({ error: (e as Error).message });
  }
}

export async function loadCrates(): Promise<void> {
  const [crates, sets, smart] = await Promise.allSettled([api.library.crates({ quiet: true }), api.sets.list({ quiet: true }), api.library.smartCrates({ quiet: true })]);
  useLibrary.setState((s) => ({
    crates: crates.status === "fulfilled" ? crates.value : s.crates,
    sets: sets.status === "fulfilled" ? sets.value : s.sets,
    smartCrates: smart.status === "fulfilled" ? smart.value.crates.map((c) => ({ id: c.id, name: c.name, filter: cleanFilter(c.filter) })) : s.smartCrates,
  }));
}

export async function saveSmartCrates(crates: SmartCrate[]): Promise<void> {
  useLibrary.setState({ smartCrates: crates });
  await api.library.saveSmartCrates(crates.map((c) => ({ id: c.id, name: c.name, filter: { ...c.filter } })));
}

/** Replace these rows in place (and drop the ones that are gone). */
export function patchRows(rows: LibraryRow[], fresh: LibraryRow[], asked: string[]): LibraryRow[] {
  const byHash = new Map(fresh.map((r) => [r.hash, r]));
  const gone = new Set(asked.filter((h) => !byHash.has(h)));
  const out: LibraryRow[] = [];
  for (const r of rows) {
    if (gone.has(r.hash)) continue;
    out.push(byHash.get(r.hash) ?? r);
    byHash.delete(r.hash);
  }
  return byHash.size ? [...out, ...byHash.values()] : out;
}

// ------------------------------------------------------------------------------------------------ live updates

const stale = new Set<string>();
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
const marks = new Map<string, "queued" | "analysing">();
let markTimer: ReturnType<typeof setTimeout> | undefined;

export function refreshSoon(hash: string) {
  stale.add(hash);
  refreshTimer ??= setTimeout(() => {
    refreshTimer = undefined;
    const hashes = [...stale];
    stale.clear();
    void api.library.view(hashes, { quiet: true }).then(
      ({ items }) => useLibrary.setState((s) => (s.rows ? { rows: patchRows(s.rows, items, hashes) } : {})),
      () => {},
    );
  }, 400);
}

/** Show a job starting or queueing on its row right away (batched per 200 ms, one array copy per batch). A track
 * the page doesn't have yet (one a download has just imported, while its batch goes on) is read in, unless an
 * import is running: the library reloads when that one finishes. */
function markSoon(hash: string, state: "queued" | "analysing") {
  if (marks.get(hash) !== "analysing") marks.set(hash, state);
  markTimer ??= setTimeout(() => {
    markTimer = undefined;
    const m = new Map(marks);
    marks.clear();
    const unknown = new Set(m.keys());
    useLibrary.setState((s) => {
      if (!s.rows) return {};
      let changed = false;
      const rows = s.rows.map((r) => {
        unknown.delete(r.hash);
        const st = m.get(r.hash);
        if (!st || r.status.state === st || (st === "queued" && r.status.state === "analysing")) return r;
        changed = true;
        return { ...r, status: { ...r.status, state: st } };
      });
      return changed ? { rows } : {};
    });
    const importing = ["queued", "running"].includes(useImports.getState().status.state);
    if (useLibrary.getState().rows && !importing) for (const h of unknown) refreshSoon(h);
  }, 200);
}

let started = false;
/** Load the library and keep it current (once, from the library page; it stays loaded after). */
export function followLibrary(): void {
  if (started) return;
  started = true;
  void loadLibrary();
  void loadCrates();
  let generation = useImports.getState().generation;
  useImports.subscribe((s) => {
    if (s.generation === generation) return;
    generation = s.generation;
    void loadLibrary();
    void loadCrates();
  });
  onServerEvent((ev) => {
    if (!ev.type.startsWith("job.")) return;
    const d = ev.data as { kind?: string; payload?: { hash?: unknown }; state?: string };
    const hash = typeof d?.payload?.hash === "string" ? d.payload.hash : null;
    if (!hash) return;
    if (ev.type === "job.queued") markSoon(hash, "queued");
    else if (ev.type === "job.started") markSoon(hash, "analysing");
    else if (ev.type === "job.done" || ev.type === "job.failed" || ev.type === "job.cancelled") {
      if (ev.type === "job.done" && d.kind === "waveform") overviews.invalidate(hash);
      refreshSoon(hash);
    }
  });
}

// ------------------------------------------------------------------------------------------------ readiness (#100)

/** Queue a track's missing (or failed) analyses again. */
export async function retryAnalysis(row: LibraryRow): Promise<void> {
  const kinds = new Set([...row.status.missing, ...Object.entries(row.status.jobs).flatMap(([k, v]) => (v === "failed" ? [k] : []))]);
  for (const kind of kinds) await api.jobs.submit(kind, { hash: row.hash }, { quiet: true });
  markSoon(row.hash, "queued");
}

/** "Use anyway" for a failed track (or not any more): kept with the track; its row comes back. */
export async function setUseAnyway(hash: string, on: boolean): Promise<void> {
  const row = await api.library.useAnyway(hash, on);
  useLibrary.setState((s) => (s.rows ? { rows: patchRows(s.rows, [row], [hash]) } : {}));
}

/** The library rows of these tracks that can't go into a set or mix (tracks the page hasn't loaded aren't judged:
 * the API still refuses them). */
export function unusableRows(hashes: string[]): LibraryRow[] {
  const rows = useLibrary.getState().rows;
  if (!rows) return [];
  const want = new Set(hashes);
  return rows.filter((r) => want.has(r.hash) && !readiness(r).usable);
}
