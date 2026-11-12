import { AlertTriangle, FileUp, FolderPlus, Library, RotateCw, SearchX, Settings } from "lucide-react";
import { type MouseEvent, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button, EmptyState, Input, Page, PageBody, PageHeader, ProgressBar } from "@/components/ui";
import { CrateList, sameScope, type Scope } from "@/features/library/CrateList";
import { EMPTY_FILTER, filterRows, type LibraryFilter, type Sort, type SortKey, sortRows } from "@/features/library/filter";
import { FilterBar } from "@/features/library/FilterBar";
import { FailedAnalysisBar } from "@/features/library/FailedAnalysisBar";
import { LibrarySkeleton, LibraryTable } from "@/features/library/LibraryTable";
import { KeyEditor } from "@/features/library/KeyEditor";
import { plural } from "@/lib/format";
import { navigate, useHashQuery } from "@/lib/router";
import { importFix, importSummary, useImports } from "@/store/imports";
import { followLibrary, loadCrates, loadLibrary, saveSmartCrates, useLibrary } from "@/store/library";
import { toast } from "@/store/toasts";

/** The latest import: a progress bar while it runs, its summary once done, what to do about it if it failed. */
function ImportBar({ onChooseXml }: { onChooseXml: () => void }) {
  const s = useImports((st) => st.status);
  if (s.state === "idle") return null;
  const running = s.state === "running" || s.state === "queued";
  const what = s.kind === "watch" ? `Scanning ${s.folder ?? "watch folders"}` : s.kind === "xml" ? `Importing playlists from ${s.label}` : `Importing ${s.label ?? "files"}`;
  if (s.state === "failed") {
    return (
      <div className="flex items-center gap-2.5 border-b border-line bg-danger/10 px-5 py-2 text-sm" role="alert">
        <AlertTriangle size={15} className="shrink-0 text-danger" />
        <span className="min-w-0 flex-1">
          <span className="font-medium text-fg">Import failed: {s.error}.</span> <span className="text-fg-muted">{importFix(s)}</span>
        </span>
        {s.kind === "xml" && (
          <Button size="sm" icon={FileUp} onClick={onChooseXml}>
            Choose a file…
          </Button>
        )}
        {s.kind === "watch" && (
          <Button size="sm" icon={Settings} onClick={() => navigate({ name: "settings" })}>
            Open Settings
          </Button>
        )}
      </div>
    );
  }
  return (
    <div className="flex items-center gap-3 border-b border-line bg-panel px-5 py-2 text-sm">
      {running ? (
        <>
          <span className="min-w-0 max-w-[50%] truncate text-fg-muted">{what}</span>
          <ProgressBar className="flex-1" value={s.total ? (s.done ?? 0) / s.total : null} label="Import progress" />
          <span className="shrink-0 font-mono text-xs text-fg-subtle">{s.total ? `${s.done ?? 0}/${s.total}` : "…"}</span>
        </>
      ) : (
        <span className="text-fg-muted">{`${s.kind === "watch" ? "Watch folders" : (s.label ?? "Import")}: ${importSummary(s)}`}</span>
      )}
    </div>
  );
}

export function LibraryPage() {
  useEffect(followLibrary, []);
  const rows = useLibrary((s) => s.rows);
  const crates = useLibrary((s) => s.crates);
  const sets = useLibrary((s) => s.sets);
  const smartCrates = useLibrary((s) => s.smartCrates);
  const loadError = useLibrary((s) => s.error);
  const [xmlPath, setXmlPath] = useState<string | null>(null);
  const [filter, setFilter] = useState<LibraryFilter>(EMPTY_FILTER);
  const [scope, setScope] = useState<Scope>({ kind: "all" });
  const [scopeHashes, setScopeHashes] = useState<Set<string> | null>(null);
  const [sort, setSort] = useState<Sort>({ key: "artist", dir: "asc" });
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const anchor = useRef<number | null>(null);

  // #/library?show=not-ready (the top bar's analysis indicator): the whole library, only the tracks that aren't ready
  const show = useHashQuery().get("show");
  useEffect(() => {
    if (show !== "not-ready") return;
    setScope({ kind: "all" });
    setFilter({ ...EMPTY_FILTER, unanalysed: true });
    navigate({ name: "library" }, { replace: true });
  }, [show]);

  // a crate or set narrows the table to its tracks (re-read when its track count changes: something was dropped on it)
  const scopeCount = scope.kind === "crate" ? crates.find((c) => c.id === scope.id)?.track_count : scope.kind === "set" ? sets.find((x) => x.id === scope.id)?.track_count : undefined;
  useEffect(() => {
    if (scope.kind !== "crate" && scope.kind !== "set") {
      setScopeHashes(null);
      return;
    }
    const ctrl = new AbortController();
    const load = scope.kind === "crate" ? api.library.crateHashes(scope.id, { quiet: true, signal: ctrl.signal }) : api.sets.hashes(scope.id, { quiet: true, signal: ctrl.signal });
    load.then((r) => setScopeHashes(new Set(r.hashes)), () => {});
    return () => ctrl.abort();
  }, [scope, scopeCount]);

  const deferredFilter = useDeferredValue(filter);
  const shown = useMemo(() => (rows ? sortRows(filterRows(rows, deferredFilter, scopeHashes), sort) : []), [rows, deferredFilter, scopeHashes, sort]);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const onSelect = useCallback((hash: string, index: number, e: MouseEvent) => {
    const list = shownRef.current;
    if (e.shiftKey && anchor.current !== null) {
      const [a, b] = [Math.min(anchor.current, index), Math.max(anchor.current, index)];
      setSelected(new Set(list.slice(a, b + 1).map((r) => r.hash)));
      return;
    }
    anchor.current = index;
    if (e.metaKey || e.ctrlKey) {
      setSelected((s) => {
        const next = new Set(s);
        if (!next.delete(hash)) next.add(hash);
        return next;
      });
    } else setSelected(new Set([hash]));
  }, []);
  const dragHashes = useCallback((hash: string) => {
    const sel = selectedRef.current;
    return sel.has(hash) ? shownRef.current.filter((r) => sel.has(r.hash)).map((r) => r.hash) : [hash];
  }, []);

  const onFilter = (f: LibraryFilter) => {
    setFilter(f);
    if (scope.kind === "smart") setScope({ kind: "all" });
  };
  const onScope = (s: Scope) => {
    if (s.kind === "smart") {
      const c = smartCrates.find((x) => x.id === s.id);
      if (c) setFilter(c.filter);
    } else if (scope.kind === "smart") setFilter(EMPTY_FILTER);
    if (!sameScope(s, scope)) setSelected(new Set());
    setScope(s);
  };
  const onSaveSmart = (name: string) => {
    const id = `s${Date.now().toString(36)}`;
    void saveSmartCrates([...smartCrates, { id, name, filter }]).then(
      () => setScope({ kind: "smart", id }),
      () => toast.error("Couldn't save the smart crate"),
    );
  };
  const onDeleteSmart = (id: string) => {
    void saveSmartCrates(smartCrates.filter((c) => c.id !== id)).catch(() => toast.error("Couldn't delete the smart crate"));
    if (scope.kind === "smart" && scope.id === id) onScope({ kind: "all" });
  };
  const onDrop = async (target: "crate" | "set", id: number | null, hashes: string[]) => {
    const what = plural(hashes.length, "track");
    try {
      if (id === null) {
        const name = `${target === "crate" ? "New crate" : "New set"} ${new Date().toLocaleDateString(undefined, { day: "numeric", month: "short" })}`;
        const made = target === "crate" ? await api.library.newCrate(name, hashes) : await api.sets.create(name, hashes);
        toast.success(`${what} in “${made.name}”`, { title: target === "crate" ? "New crate" : "New set" });
      } else {
        const r = target === "crate" ? await api.library.addToCrate(id, hashes) : await api.sets.addTracks(id, hashes);
        const name = (target === "crate" ? crates : sets).find((x) => x.id === id)?.name ?? target;
        toast.success(r.added === hashes.length ? `${what} added to “${name}”` : `${plural(r.added, "track")} added to “${name}”, ${hashes.length - r.added} already in it`);
      }
    } catch {
      return; // the client toasted the error
    }
    void loadCrates();
  };

  const addFolder = async () => {
    try {
      const { paths } = await api.library.choose("folder", { quiet: true });
      if (paths[0]) {
        await api.library.addFolder(paths[0]);
        toast.info(`Watching ${paths[0]}`, { key: "library.import", title: "Watch folders" });
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) toast.info("Add watch folders by path in Settings", { title: "No folder dialog in a browser" });
    }
  };
  const importXml = async (path?: string) => {
    try {
      const chosen = path ?? (await api.library.choose("xml", { quiet: true })).paths[0];
      if (chosen) {
        await api.library.importXml(chosen);
        setXmlPath(null);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) setXmlPath(""); // a browser: ask for the path
    }
  };

  const total = rows?.length ?? null;
  const empty = rows !== null && !rows.length;
  const subtitle =
    total === null
      ? loadError
        ? "Not loaded"
        : "…"
      : `${plural(total, "track")}${shown.length !== total ? ` · ${shown.length} shown` : ""}${selected.size > 1 ? ` · ${selected.size} selected` : ""} · drag tracks onto a crate or set · drop files anywhere to add them`;
  return (
    <Page>
      <div className="flex min-h-0 flex-1">
        <CrateList scope={scope} onScope={onScope} total={total} crates={crates} sets={sets} smartCrates={smartCrates} onDeleteSmart={onDeleteSmart} onDrop={(t, id, h) => void onDrop(t, id, h)} />
        <div className="flex min-w-0 flex-1 flex-col">
          <PageHeader
            title={scope.kind === "crate" ? (crates.find((c) => c.id === scope.id)?.name ?? "Crate") : scope.kind === "set" ? (sets.find((x) => x.id === scope.id)?.name ?? "Set") : scope.kind === "smart" ? (smartCrates.find((c) => c.id === scope.id)?.name ?? "Smart crate") : "Library"}
            subtitle={subtitle}
            actions={
              <>
                <Button icon={FolderPlus} onClick={() => void addFolder()}>
                  Watch a folder…
                </Button>
                <Button icon={FileUp} onClick={() => void importXml()}>
                  Import Rekordbox / iTunes XML…
                </Button>
              </>
            }
          >
            <FilterBar filter={filter} onChange={onFilter} onSave={onSaveSmart} />
            {xmlPath !== null && (
              <form
                className="flex items-center gap-2 pb-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  void importXml(xmlPath);
                }}
              >
                <Input wrapperClassName="flex-1" autoFocus placeholder="Path to rekordbox.xml or Library.xml" value={xmlPath} onChange={(e) => setXmlPath(e.target.value)} aria-label="XML path" />
                <Button type="submit" variant="primary" disabled={!xmlPath.trim()}>
                  Import
                </Button>
              </form>
            )}
          </PageHeader>
          <ImportBar onChooseXml={() => void importXml()} />
          {rows && <FailedAnalysisBar rows={rows} />}
          <PageBody padded={false} className="flex flex-col">
            {rows === null ? (
              loadError ? (
                <EmptyState
                  className="h-full"
                  tone="danger"
                  icon={Library}
                  title="Couldn't load your library"
                  description="Your music and analyses are safe on disk. Try again; if it keeps failing, quit Seedbox 2 and open it again."
                  action={
                    <Button variant="primary" icon={RotateCw} onClick={() => void loadLibrary()}>
                      Try again
                    </Button>
                  }
                />
              ) : (
                <LibrarySkeleton className="flex-1" />
              )
            ) : empty ? (
              <EmptyState
                className="h-full"
                icon={Library}
                title="No music yet"
                description="Drop files or folders onto the window, watch a folder, or import your Rekordbox or iTunes/Music playlists. Every track is analysed as it comes in."
                action={
                  <>
                    <Button variant="primary" icon={FolderPlus} onClick={() => void addFolder()}>
                      Watch a folder…
                    </Button>
                    <Button icon={FileUp} onClick={() => void importXml()}>
                      Import playlists…
                    </Button>
                  </>
                }
              />
            ) : rows !== null && !shown.length ? (
              <EmptyState className="h-full" icon={SearchX} title="Nothing matches" description="No track here fits these filters." action={<Button onClick={() => onFilter(EMPTY_FILTER)}>Clear the filters</Button>} />
            ) : (
              <LibraryTable className="flex-1" rows={shown} sort={sort} onSort={(key: SortKey) => setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "energy" || key === "added" ? "desc" : "asc" }))} selected={selected} onSelect={onSelect} dragHashes={dragHashes} />
            )}
          </PageBody>
        </div>
      </div>
      <KeyEditor />
    </Page>
  );
}
