// OWNER: FRONTEND-CRATE agent. Search → version picker → #/crate/<seed_id>.
import { ArrowRight, ChevronDown, History, Link2, Search, Sprout, X } from "lucide-react";
import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { SearchPage as SearchResult, Track } from "@/api/types";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Button, Checkbox, CoverArt, EmptyState, Input, Kbd, KeyBadge, Page, Spinner } from "@/components/ui";
import { ErrorState } from "@/features/crate/ErrorState";
import { looksLikeTrackRef } from "@/features/crate/trackMeta";
import { ResultsTable } from "@/features/search/ResultsTable";
import { clearRecent, loadGroupPref, loadRecent, pushRecent, saveGroupPref, searchMemo } from "@/features/search/session";
import { buildRows } from "@/features/search/versions";
import { cn } from "@/lib/cn";
import { FOCUS_SEARCH_EVENT, takePendingFocus } from "@/lib/focusSearch";
import { fmtBpm, plural } from "@/lib/format";
import { useHotkey } from "@/lib/hotkeys";
import { navigate, setHashQuery, useHashQuery } from "@/lib/router";
import { useListNav } from "@/lib/useListNav";
import { useApp } from "@/store/app";

type Status = "idle" | "loading" | "more";

function openSeed(t: Track) {
  useApp.getState().select({ kind: "preview", track: t });
  navigate({ name: "crate", seedId: t.id });
}

export function SearchPage() {
  const query = useHashQuery();
  const q = query.get("q")?.trim() ?? "";
  const [initial] = useState(() => {
    const m = searchMemo.get();
    return m && (!q || m.term === q) ? m : null;
  });
  const [draft, setDraft] = useState(initial?.term ?? q);
  const [term, setTerm] = useState(initial?.term ?? "");
  const [pages, setPages] = useState<SearchResult[]>(initial?.pages ?? []);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<unknown>(null);
  const [selectedId, setSelectedId] = useState<string | null>(initial?.selectedId ?? null);
  const [group, setGroup] = useState(loadGroupPref);
  const [recent, setRecent] = useState(loadRecent);
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrollTop = useRef(initial?.scrollTop ?? 0);
  const ctrlRef = useRef<AbortController | null>(null);
  const termRef = useRef(term);
  termRef.current = term;

  usePageTitle("Search", term || null);

  const items = useMemo(() => {
    const seen = new Set<string>();
    return pages.flatMap((p) => p.items).filter((t) => (seen.has(t.id) ? false : (seen.add(t.id), true)));
  }, [pages]);
  const rows = useMemo(() => buildRows(items, group), [items, group]);
  const ids = useMemo(() => rows.map((r) => r.track.id), [rows]);
  const last = pages[pages.length - 1];
  const total = last?.total_count ?? 0;
  const hasNext = !!last?.has_next;

  const run = useCallback(async (raw: string, page = 1) => {
    const t = raw.trim();
    if (!t) return;
    ctrlRef.current?.abort();
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    setError(null);
    if (page === 1) {
      setStatus("loading");
      setTerm(t);
      setDraft(raw);
    } else setStatus("more");
    try {
      const res = await api.search(t, page, { quiet: true, signal: ctrl.signal });
      if (ctrl.signal.aborted) return;
      if (page === 1) {
        setPages([res]);
        setSelectedId(res.items[0]?.id ?? null);
        scrollTop.current = 0;
        scrollRef.current?.scrollTo({ top: 0 });
        setRecent(pushRecent(t));
        if (t !== new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("q")) setHashQuery({ q: t });
      } else {
        setPages((prev) => [...prev.filter((p) => p.page !== res.page), res].sort((a, b) => a.page - b.page));
      }
    } catch (e) {
      if (ctrl.signal.aborted || (e as Error).name === "AbortError") return;
      setError(e);
      if (page === 1) setPages([]);
    } finally {
      if (!ctrl.signal.aborted) setStatus("idle");
    }
  }, []);

  const loadMore = useCallback(() => {
    if (status !== "idle" || !hasNext || !last) return;
    void run(termRef.current, last.page + 1);
  }, [status, hasNext, last, run]);

  // URL → search (deep links, reload, typing a new ?q=)
  useEffect(() => {
    if (q && q !== termRef.current) void run(q);
  }, [q, run]);

  // restore scroll + keep the session memo current
  useEffect(() => {
    if (initial && scrollRef.current) scrollRef.current.scrollTop = initial.scrollTop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (term) searchMemo.set({ term, pages, selectedId, scrollTop: scrollTop.current });
  }, [term, pages, selectedId]);
  useEffect(() => () => ctrlRef.current?.abort(), []);

  // focus: on mount (autofocus unless we came back to a result list), ⌘K, "/"
  const focusInput = useCallback((selectAll = true) => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    if (selectAll) el.select();
  }, []);
  useEffect(() => {
    const pending = takePendingFocus();
    if (pending || !initial) focusInput(pending);
    const onFocus = () => focusInput(true);
    window.addEventListener(FOCUS_SEARCH_EVENT, onFocus);
    return () => window.removeEventListener(FOCUS_SEARCH_EVENT, onFocus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useHotkey(["/", "Mod+f"], () => focusInput(true));

  const select = useCallback(
    (id: string) => {
      setSelectedId(id);
      const t = items.find((x) => x.id === id);
      if (t) useApp.getState().select({ kind: "preview", track: t });
      if (id === ids[ids.length - 1] && hasNext) loadMore();
    },
    [items, ids, hasNext, loadMore],
  );
  const selectTrack = useCallback((t: Track) => select(t.id), [select]);
  const selected = items.find((t) => t.id === selectedId) ?? null;

  useListNav(ids, selectedId, select);
  // ↑ on the first row goes back to the search box
  useHotkey(["ArrowUp", "k"], () => {
    if (!ids.length || selectedId === ids[0]) {
      focusInput(false);
      return;
    }
    return false;
  });
  useHotkey("Enter", () => void (selected && openSeed(selected)), { enabled: !!selected });
  useHotkey("Mod+Enter", () => void (selected && openSeed(selected)), { enabled: !!selected, allowInInputs: true });

  const onInputKey = (e: KeyboardEvent<HTMLInputElement>) => {
    const move = (delta: number) => {
      e.preventDefault();
      if (!ids.length) return;
      const i = selectedId ? ids.indexOf(selectedId) : -1;
      select(ids[Math.max(0, Math.min(ids.length - 1, i + delta))]);
      if (delta > 0) inputRef.current?.blur(); // the list takes over: Space previews, Enter opens
    };
    if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      if (draft.trim() && (draft.trim() !== term || error || !items.length)) void run(draft);
      else if (selected) openSeed(selected);
    } else if (e.key === "ArrowDown") move(selectedId ? 1 : 0);
    else if (e.key === "PageDown") move(10);
    else if (e.key === "Escape") {
      e.preventDefault();
      if (draft) setDraft("");
      else inputRef.current?.blur();
    }
  };

  const loading = status === "loading";
  const showResults = items.length > 0;

  return (
    <Page>
      <div className="shrink-0 border-b border-line bg-surface px-5 pt-4 pb-3">
        <div className="flex items-center gap-2">
          <Input
            ref={inputRef}
            size="lg"
            icon={Search}
            value={draft}
            spellCheck={false}
            autoComplete="off"
            placeholder="Search tunebat — artist and title, or paste a Spotify / tunebat link"
            aria-label="Search tunebat"
            wrapperClassName="h-11 max-w-[820px] flex-1 text-md shadow-card"
            onChange={(e) => {
              const v = e.target.value;
              setDraft(v);
              if (looksLikeTrackRef(v) && v.trim() !== term) void run(v);
            }}
            onKeyDown={onInputKey}
            suffix={
              <span className="flex items-center gap-1.5">
                {loading ? <Spinner size={14} className="text-accent-hover" /> : null}
                {draft && !loading && (
                  <button
                    type="button"
                    aria-label="Clear"
                    className="grid size-5 place-items-center rounded-full text-fg-subtle hover:bg-overlay hover:text-fg"
                    onClick={() => {
                      setDraft("");
                      focusInput(false);
                    }}
                  >
                    <X size={13} />
                  </button>
                )}
                <Kbd combo="Mod+K" subtle />
              </span>
            }
          />
          <Button size="lg" variant="secondary" className="h-11" disabled={!draft.trim()} loading={loading} onClick={() => void run(draft)}>
            Search
          </Button>
        </div>
        <div className="mt-2.5 flex h-5 items-center gap-3 text-xs text-fg-subtle">
          {term && !error && pages.length > 0 ? (
            <span className="num">
              {items.length < total ? (
                <>
                  <span className="text-fg-muted">{items.length}</span> of <span className="text-fg-muted">{total.toLocaleString()}</span> results
                </>
              ) : (
                <span className="text-fg-muted">{plural(items.length, "result")}</span>
              )}{" "}
              for <span className="text-fg">“{term}”</span>
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5">
              <Link2 size={12} /> Paste a Spotify or tunebat link to jump straight to that exact version
            </span>
          )}
          <span className="flex-1" />
          {items.length > 0 && (
            <>
              <Checkbox
                checked={group}
                onChange={(v) => {
                  setGroup(v);
                  saveGroupPref(v);
                }}
                label={<span className="text-xs">Group versions</span>}
              />
              <span className="h-3 w-px bg-line-strong" />
              <span className="hidden items-center gap-1 lg:inline-flex">
                <Kbd combo="ArrowDown" subtle />
                <Kbd combo="ArrowUp" subtle /> select
                <Kbd combo="Space" subtle className="ml-1.5" /> preview
                <Kbd combo="Enter" subtle className="ml-1.5" /> use as seed
              </span>
            </>
          )}
        </div>
      </div>

      {error && !items.length ? (
        <ErrorState className="flex-1" error={error} what="that search" onRetry={() => void run(term || draft)} />
      ) : showResults ? (
        <div className={cn("relative flex min-h-0 flex-1 flex-col transition-opacity", loading && "opacity-50")}>
          <ResultsTable
            rows={rows}
            selectedId={selectedId}
            onSelect={selectTrack}
            onOpen={openSeed}
            grouped={group}
            scrollRef={scrollRef}
            onScroll={(top) => {
              scrollTop.current = top;
              searchMemo.patch({ scrollTop: top });
            }}
            footer={
              <div className="flex h-14 items-center justify-center gap-3 border-t border-line/60 text-xs text-fg-subtle">
                {error ? (
                  <span className="text-danger">Couldn't load more — </span>
                ) : null}
                {hasNext ? (
                  <Button size="sm" icon={ChevronDown} loading={status === "more"} onClick={loadMore}>
                    Load {Math.min(50, total - items.length) || 50} more
                  </Button>
                ) : (
                  <span className="num">End of results · {plural(items.length, "track")}</span>
                )}
              </div>
            }
          />
        </div>
      ) : loading ? (
        <SkeletonRows />
      ) : term && pages.length ? (
        <EmptyState
          className="flex-1"
          icon={Search}
          title={`Nothing on tunebat for “${term}”`}
          description="Check the spelling, try fewer words (just artist + title), or paste the track's Spotify link."
        />
      ) : (
        <StartPanel
          recent={recent}
          onPick={(t) => void run(t)}
          onClearRecent={() => {
            clearRecent();
            setRecent([]);
          }}
        />
      )}
    </Page>
  );
}

function SkeletonRows() {
  return (
    <div className="flex-1 overflow-hidden" aria-busy>
      <div className="h-7 border-b border-line bg-panel" />
      {Array.from({ length: 14 }, (_, i) => (
        <div key={i} className="flex h-[34px] items-center gap-3 border-b border-line/60 px-5" style={{ opacity: 1 - i * 0.06 }}>
          <span className="size-[22px] rounded-full bg-raised" />
          <span className="size-[26px] rounded-xs bg-raised" />
          <span className="h-2.5 rounded-xs bg-raised animate-pulse-soft" style={{ width: `${18 + ((i * 37) % 14)}%` }} />
          <span className="h-2.5 w-[12%] rounded-xs bg-raised/70" />
          <span className="h-2.5 w-[14%] rounded-xs bg-raised/60" />
          <span className="flex-1" />
          <span className="h-2.5 w-10 rounded-xs bg-raised/70" />
          <span className="h-[18px] w-8 rounded-xs bg-raised" />
          <span className="h-2.5 w-7 rounded-xs bg-raised/70" />
        </div>
      ))}
    </div>
  );
}

function StartPanel({ recent, onPick, onClearRecent }: { recent: string[]; onPick: (t: string) => void; onClearRecent: () => void }) {
  const crates = useApp((s) => s.crates);
  const seeds = useMemo(
    () =>
      [...(crates ?? [])]
        .filter((c) => c.seed)
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .slice(0, 6),
    [crates],
  );
  return (
    <div className="min-h-0 flex-1 overflow-auto px-5 py-8 scroll-thin">
      <div className="flex max-w-[820px] flex-col gap-8">
        <div className="flex flex-col gap-1.5">
          <h2 className="text-lg font-semibold tracking-tight">Find a seed track</h2>
          <p className="max-w-[560px] text-sm text-fg-muted">
            Pick the exact version you play — single, extended mix or remix. Seedbox ranks tunebat's recommendations for it by how well they mix,
            pre-ticks the ones that pass your rule, and preloads 20-second previews.
          </p>
        </div>

        {recent.length > 0 && (
          <section className="flex flex-col gap-2">
            <div className="label-caps flex items-center gap-2">
              <History size={11} /> Recent searches
              <button type="button" className="ml-auto text-2xs font-medium tracking-normal text-fg-faint normal-case hover:text-fg-muted" onClick={onClearRecent}>
                Clear
              </button>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {recent.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => onPick(r)}
                  className="h-7 max-w-[280px] truncate rounded-sm border border-line-strong bg-raised px-2.5 text-sm text-fg-muted transition-colors hover:border-fg-faint hover:text-fg"
                >
                  {r}
                </button>
              ))}
            </div>
          </section>
        )}

        {seeds.length > 0 && (
          <section className="flex flex-col gap-2">
            <div className="label-caps flex items-center gap-2">
              <Sprout size={11} /> Your seeds — open recommendations again
            </div>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(250px,1fr))] gap-2">
              {seeds.map((c) => (
                <button
                  key={c.folder}
                  type="button"
                  onClick={() => openSeed(c.seed!)}
                  className="group flex items-center gap-2.5 rounded-md border border-line bg-panel p-2 text-left transition-colors hover:border-line-strong hover:bg-raised/50"
                >
                  <CoverArt src={c.seed!.cover_url} size={36} rounded="xs" camelot={c.seed!.camelot} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{c.seed!.name}</span>
                    <span className="block truncate text-xs text-fg-muted">{c.seed!.artists.join(", ")}</span>
                  </span>
                  <span className="flex shrink-0 flex-col items-end gap-0.5">
                    <KeyBadge camelot={c.seed!.camelot} size="xs" />
                    <span className="text-2xs text-fg-subtle num">{fmtBpm(c.seed!.bpm)}</span>
                  </span>
                  <ArrowRight size={14} className="shrink-0 text-fg-faint group-hover:text-fg-muted" />
                </button>
              ))}
            </div>
          </section>
        )}

        <section className="flex flex-col gap-2">
          <div className="label-caps">Tips</div>
          <ul className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm text-fg-muted">
            <li className="flex items-center gap-2">
              <Kbd combo="Mod+K" subtle /> jumps here from anywhere
            </li>
            <li className="flex items-center gap-2">
              <Kbd combo="Space" subtle /> previews the selected result
            </li>
            <li className="flex items-center gap-2">
              <Kbd combo="ArrowDown" subtle /> from the box moves into the results
            </li>
            <li className="flex items-center gap-2">
              <Kbd combo="Enter" subtle /> uses the selected version as the seed
            </li>
          </ul>
        </section>
      </div>
    </div>
  );
}
