// OWNER: FRONTEND-CRATE agent. Seed → ranked recommendations → tick → Download.
import { ArrowLeft, ArrowRight, CircleSlash, Download, Headphones, Sprout } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, MOCK } from "@/api/client";
import type { Crate, Track } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Badge, Button, IconButton, KeyBadge, MoveChip, Page, ProgressBar, Spinner, Tabs, Tooltip } from "@/components/ui";
import { ErrorState } from "@/features/crate/ErrorState";
import { usePreviewCounts, useSyncPreviewStatuses } from "@/features/crate/PreviewControls";
import { type CrateRow, RecsTable, type SortKey } from "@/features/crate/RecsTable";
import { SeedHeader } from "@/features/crate/SeedHeader";
import { crateMemo, defaultTicks, selectionMemo, tickMemo } from "@/features/crate/session";
import { pitchVsSeed } from "@/features/crate/trackMeta";
import { parseCamelot } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { fmtTime, plural } from "@/lib/format";
import { useHotkey } from "@/lib/hotkeys";
import { href, navigate, useHashQuery } from "@/lib/router";
import { useListNav } from "@/lib/useListNav";
import { useActiveJobs, useApp, usePreview, useSetVersion } from "@/store/app";

type Filter = "all" | "passing" | "ticked";
const FOLLOW_KEY = "seedbox.crate.follow";

function loadFollow(): boolean {
  try {
    return localStorage.getItem(FOLLOW_KEY) !== "0";
  } catch {
    return true;
  }
}

function keyOrder(code: string): number {
  const p = parseCamelot(code);
  return p ? p.n * 2 + (p.letter === "B" ? 1 : 0) : 99;
}

function sortValue(r: CrateRow, key: SortKey, seed: Track): number | string {
  const t = r.track;
  switch (key) {
    case "score":
      return r.rec?.score.value ?? 101;
    case "title":
      return t.name.toLowerCase();
    case "key":
      return keyOrder(t.camelot);
    case "bpm":
      return t.bpm;
    case "pitch":
      return Math.abs(pitchVsSeed(seed.bpm, t.bpm)?.pct ?? 99);
    case "energy":
      return t.energy;
    case "length":
      return t.duration_s;
  }
}

export function CratePage({ seedId }: { seedId: string }) {
  const demoParam = useHashQuery().get("demo");
  const demo = MOCK ? demoParam : null;
  const [crate, setCrate] = useState<Crate | undefined>(() => crateMemo.get(seedId));
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(!crate);
  const [reloadTick, setReloadTick] = useState(0);
  const folderVersion = useSetVersion(crate?.folder ?? "");

  // fetch (and refetch after a download into this crate changed what's already in it)
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .newCrate(seedId, { quiet: true, signal: ctrl.signal })
      .then((c) => {
        if (ctrl.signal.aborted) return;
        crateMemo.set(seedId, c);
        setCrate(c);
      })
      .catch((e: Error) => {
        if (ctrl.signal.aborted || e.name === "AbortError") return;
        setError(e);
      })
      .finally(() => !ctrl.signal.aborted && setLoading(false));
    return () => ctrl.abort();
  }, [seedId, reloadTick, folderVersion]);

  usePageTitle("Search", crate?.seed.display_name ?? "Loading…");

  if (!crate) {
    return (
      <Page>
        {error ? (
          <ErrorState
            className="flex-1"
            error={error}
            what="that seed on tunebat"
            onRetry={() => setReloadTick((n) => n + 1)}
            action={
              <Button icon={ArrowLeft} onClick={() => navigate({ name: "search" })}>
                Back to search
              </Button>
            }
          />
        ) : (
          <CrateSkeleton />
        )}
      </Page>
    );
  }
  return <CrateView key={crate.seed.id} seedId={seedId} crate={crate} refreshing={loading} demo={demo} />;
}

function CrateView({ seedId, crate, refreshing, demo }: { seedId: string; crate: Crate; refreshing: boolean; demo: string | null }) {
  const seed = crate.seed;
  const [ticks, setTicksState] = useState<Set<string>>(() => tickMemo.get(seedId) ?? defaultTicks(crate));
  const [selectedId, setSelectedId] = useState<string | null>(() => selectionMemo.get(seedId) ?? seed.id);
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "score", dir: "desc" });
  const [filter, setFilter] = useState<Filter>("all");
  const [follow, setFollow] = useState(loadFollow);
  const [starting, setStarting] = useState(false);
  const lastTicked = useRef<string | null>(null);
  const library = useApp((s) => s.crates?.find((c) => c.folder === crate.folder));
  const activeJob = useActiveJobs().find((j) => j.folder === crate.folder);

  const setTicks = useCallback(
    (fn: (prev: Set<string>) => Set<string>) =>
      setTicksState((prev) => {
        const next = fn(prev);
        tickMemo.set(seedId, next);
        return next;
      }),
    [seedId],
  );

  const allRows: CrateRow[] = useMemo(
    () => [
      { track: seed, isSeed: true, rec: null, existingPath: crate.seed_existing_path },
      ...crate.recs.map((r) => ({ track: r.track, isSeed: false, rec: r, existingPath: r.existing_path })),
    ],
    [crate, seed],
  );
  const allIds = useMemo(() => allRows.map((r) => r.track.id), [allRows]);
  const passingIds = useMemo(() => allRows.filter((r) => r.isSeed || r.rec?.score.passed).map((r) => r.track.id), [allRows]);

  const rows = useMemo(() => {
    const [seedRow, ...rest] = allRows;
    let list = rest;
    if (filter === "passing") list = list.filter((r) => r.rec?.score.passed);
    if (filter === "ticked") list = list.filter((r) => ticks.has(r.track.id));
    if (sort.key !== "score" || sort.dir !== "desc") {
      const sign = sort.dir === "desc" ? -1 : 1;
      list = [...list].sort((a, b) => {
        const va = sortValue(a, sort.key, seed);
        const vb = sortValue(b, sort.key, seed);
        return sign * (typeof va === "string" ? va.localeCompare(vb as string) : va - (vb as number));
      });
    }
    return filter === "ticked" && !ticks.has(seed.id) ? list : [seedRow, ...list];
  }, [allRows, filter, sort, ticks, seed]);
  const ids = useMemo(() => rows.map((r) => r.track.id), [rows]);

  useSyncPreviewStatuses(allIds, true);
  const previews = usePreviewCounts(allIds);

  // ---- selection + keyboard
  const trackById = useMemo(() => new Map(allRows.map((r) => [r.track.id, r.track])), [allRows]);
  const select = useCallback(
    (id: string, fromKeyboard = false) => {
      setSelectedId(id);
      selectionMemo.set(seedId, id);
      const t = trackById.get(id);
      if (!t) return;
      useApp.getState().select({ kind: "preview", track: t });
      if (fromKeyboard && follow) {
        const p = usePlayer.getState();
        if (p.source === "preview" && (p.status === "playing" || p.status === "loading") && p.currentId !== id && allIds.includes(p.currentId ?? "")) {
          if (useApp.getState().previews[id]?.state === "no_match") player.stop();
          else void player.playPreview(t);
        }
      }
    },
    [seedId, trackById, follow, allIds],
  );
  useEffect(() => {
    const t = selectedId ? trackById.get(selectedId) : null;
    if (t) useApp.getState().select({ kind: "preview", track: t });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useListNav(ids, selectedId, (id) => select(id, true));

  const toggleTick = useCallback(
    (id: string, value: boolean, e?: MouseEvent | KeyboardEvent) => {
      // resolve the shift-click range now: React may run the updater later, after lastTicked has moved on
      const anchor = lastTicked.current;
      let range = [id];
      if (e?.shiftKey && anchor && ids.includes(anchor) && ids.includes(id)) {
        const [a, b] = [ids.indexOf(anchor), ids.indexOf(id)].sort((x, y) => x - y);
        range = ids.slice(a, b + 1);
      }
      setTicks((prev) => {
        const next = new Set(prev);
        for (const x of range) value ? next.add(x) : next.delete(x);
        return next;
      });
      lastTicked.current = id;
    },
    [ids, setTicks],
  );
  const tickPassing = () => setTicks(() => new Set(passingIds)); // "passing only": failing rows unticked
  const tickNone = () => setTicks(() => new Set());
  const tickInvert = () => setTicks((prev) => new Set(allIds.filter((x) => !prev.has(x))));
  const tickDefaults = () => setTicks(() => defaultTicks(crate));

  useHotkey(["x", "Enter"], () => void (selectedId && toggleTick(selectedId, !ticks.has(selectedId))), { enabled: !!selectedId });
  useHotkey("a", tickPassing);
  useHotkey("Shift+A", () => setTicks(() => new Set(allIds)));
  useHotkey("n", tickNone);
  useHotkey("i", tickInvert);

  // ---- download
  const ticked = useMemo(() => allRows.filter((r) => ticks.has(r.track.id)), [allRows, ticks]);
  const tickedExisting = ticked.filter((r) => r.existingPath).length;
  const tickedFailing = ticked.filter((r) => r.rec && !r.rec.score.passed).length;
  const tickedSeconds = ticked.reduce((s, r) => s + (r.track.duration_s || 0), 0);
  const startDownload = useCallback(async () => {
    if (!ticked.length || starting) return;
    setStarting(true);
    try {
      const job = await api.startJob(seed.id, ticked.map((r) => r.track.id));
      useApp.getState().upsertJob(job);
      navigate({ name: "downloads", jobId: job.job_id });
    } catch {
      /* the client already toasted (409, 422, network…) */
    } finally {
      setStarting(false);
    }
  }, [ticked, starting, seed.id]);
  useHotkey("Mod+Enter", () => void startDownload(), { enabled: ticked.length > 0 });

  // ---- mock-only screenshot demos (#/crate/<id>?demo=play | download)
  const demoDone = useRef(false);
  const demoTarget = crate.recs.find((r) => r.score.passed)?.track ?? seed;
  const demoPreview = usePreview(demoTarget.id);
  useEffect(() => {
    if (!demo || demoDone.current) return;
    if (demo === "download") {
      demoDone.current = true;
      void startDownload();
    } else if (demo === "play" && demoPreview?.state === "ready") {
      demoDone.current = true;
      select(demoTarget.id);
      void player.playPreview(demoTarget);
    }
  }, [demo, demoPreview?.state, demoTarget, select, startDownload]);

  const allState = ticks.size === 0 ? "none" : allIds.every((x) => ticks.has(x)) ? "all" : "some";
  const selectedRow = allRows.find((r) => r.track.id === selectedId) ?? null;

  return (
    <Page>
      <SeedHeader crate={crate} library={library} />

      {/* toolbar */}
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-line bg-surface px-5">
        <Tabs
          variant="segmented"
          value={filter}
          onChange={setFilter}
          items={[
            { id: "all", label: "All", count: allRows.length },
            { id: "passing", label: "Pass rule", count: passingIds.length },
            { id: "ticked", label: "Ticked", count: ticks.size },
          ]}
        />
        <span className="mx-1 h-4 w-px bg-line-strong" />
        <span className="text-xs text-fg-subtle">Tick</span>
        <Tooltip content="Tick only the seed + tracks that pass the key/BPM rule">
          <Button size="xs" variant="ghost" onClick={tickPassing} kbd="A">
            Passing only
          </Button>
        </Tooltip>
        <Button size="xs" variant="ghost" onClick={() => setTicks(() => new Set(allIds))} kbd="Shift+A">
          All
        </Button>
        <Button size="xs" variant="ghost" onClick={tickNone} kbd="N">
          None
        </Button>
        <Button size="xs" variant="ghost" onClick={tickInvert} kbd="I">
          Invert
        </Button>
        <Tooltip content="Back to the default ticks (Settings → pre-tick)">
          <Button size="xs" variant="ghost" onClick={tickDefaults}>
            Reset
          </Button>
        </Tooltip>
        <span className="flex-1" />
        {refreshing && (
          <Tooltip content="Refreshing from the engine">
            <Spinner size={12} className="text-fg-subtle" />
          </Tooltip>
        )}
        <PreviewSummary counts={previews} />
        <span className="mx-1 h-4 w-px bg-line-strong" />
        <IconButton
          icon={Headphones}
          size="sm"
          active={follow}
          label="Follow selection"
          tooltip={follow ? "Audition follows the selection: while a preview plays, ↑/↓ switch to the next row's clip (on)" : "Audition follows the selection (off)"}
          onClick={() => {
            setFollow(!follow);
            try {
              localStorage.setItem(FOLLOW_KEY, follow ? "0" : "1");
            } catch {
              /* ignore */
            }
          }}
        />
      </div>

      {rows.length > (filter === "ticked" ? 0 : 1) || filter === "all" ? (
        <RecsTable
          rows={rows}
          seed={seed}
          ticks={ticks}
          selectedId={selectedId}
          sort={sort}
          onSort={(k) => setSort((s) => (s.key === k ? { key: k, dir: s.dir === "desc" ? "asc" : "desc" } : { key: k, dir: k === "title" || k === "key" || k === "pitch" ? "asc" : "desc" }))}
          onSelect={select}
          onTick={toggleTick}
          allState={allState}
          onTickAll={(v) => (v ? setTicks(() => new Set(allIds)) : tickNone())}
        />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-fg-subtle">
          <CircleSlash size={18} />
          {filter === "ticked" ? "Nothing ticked yet — press A to tick everything that passes the rule." : "No recommendation passes the rule for this seed."}
        </div>
      )}

      {/* bulk bar */}
      <footer className="flex h-[52px] shrink-0 items-center gap-4 border-t border-line bg-panel px-5">
        <Inspector row={selectedRow} />
        <div className="flex shrink-0 items-center gap-3">
          {activeJob ? (
            <a href={href({ name: "downloads", jobId: activeJob.job_id })} className="inline-flex items-center gap-1.5 text-xs text-accent-hover hover:underline">
              <Spinner size={11} /> Downloading into this crate — view progress <ArrowRight size={12} />
            </a>
          ) : (
            <span className="text-right text-xs leading-tight text-fg-subtle num">
              <span className="text-fg-muted">{plural(ticked.length, "track")} ticked</span>
              {tickedFailing > 0 && (
                <>
                  {" · "}
                  <Tooltip content="Ticked although they fail the key/BPM rule — press A to keep only passing tracks">
                    <span className="text-warn">{tickedFailing} fail the rule</span>
                  </Tooltip>
                </>
              )}
              {tickedExisting > 0 && <span> · {tickedExisting} already in crate</span>}
              <br />
              {fmtTime(tickedSeconds)} of music
            </span>
          )}
          <Button
            variant="primary"
            size="md"
            icon={Download}
            loading={starting}
            disabled={!ticked.length}
            onClick={() => void startDownload()}
            kbd="Mod+Enter"
            className="min-w-[188px]"
          >
            Download {plural(ticked.length, "track")}
          </Button>
        </div>
      </footer>
    </Page>
  );
}

function PreviewSummary({ counts }: { counts: ReturnType<typeof usePreviewCounts> }) {
  const { total, ready, busy, noMatch, error } = counts;
  const settled = ready + noMatch + error;
  return (
    <Tooltip
      content={
        <span className="num">
          {ready} ready · {busy} loading · {noMatch} no match{error ? ` · ${error} failed` : ""} — clips preload in the background, ticked/high-score first
        </span>
      }
    >
      <span className="flex items-center gap-2 text-xs whitespace-nowrap text-fg-muted num">
        {busy > 0 ? <Spinner size={11} className="text-info" /> : <Headphones size={12} className="text-ok" />}
        <span>
          Previews <span className="text-fg">{ready}</span>
          <span className="text-fg-subtle">/{total}</span>
        </span>
        <ProgressBar value={total ? settled / total : 0} size="xs" tone={busy ? "info" : "ok"} className="w-16" />
        {noMatch > 0 && <span className="text-fg-subtle">{noMatch} no match</span>}
      </span>
    </Tooltip>
  );
}

/** One-line details for the selected row (the CLI's line under the checklist). */
function Inspector({ row }: { row: CrateRow | null }) {
  const preview = usePreview(row?.track.id);
  if (!row) return <div className="min-w-0 flex-1" />;
  const t = row.track;
  const rec = row.rec;
  return (
    <div className="flex min-w-0 flex-1 items-center gap-2.5 text-xs">
      <KeyBadge camelot={t.camelot} size="xs" />
      <span className="shrink-0 text-fg-muted">{t.key || "unknown key"}</span>
      <span className="text-fg-faint">·</span>
      {rec ? (
        <>
          <MoveChip move={rec.score.move} size="xs" showHelp={false} />
          <Badge size="xs" tone={rec.score.passed ? "ok" : "danger"} variant="soft">
            {rec.score.passed ? "passes rule" : "fails rule"}
          </Badge>
          <span className="shrink-0 font-mono text-2xs text-fg-subtle">{rec.rule_reason.replace(/\s{2,}/g, "  ")}</span>
        </>
      ) : (
        <Badge size="xs" tone="accent" icon={Sprout}>
          seed
        </Badge>
      )}
      {preview?.youtube_title && (
        <>
          <span className="text-fg-faint">·</span>
          <span className="min-w-0 truncate text-fg-subtle select-text" title={preview.youtube_title}>
            ▶ {preview.youtube_title}
          </span>
        </>
      )}
      {preview?.state === "no_match" && preview.error && (
        <>
          <span className="text-fg-faint">·</span>
          <span className="min-w-0 truncate text-fg-subtle select-text">{preview.error}</span>
        </>
      )}
    </div>
  );
}

function CrateSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col" aria-busy>
      <div className="flex shrink-0 gap-4 border-b border-line px-5 py-4">
        <div className="size-[92px] animate-pulse-soft rounded-md bg-raised" />
        <div className="flex flex-1 flex-col justify-center gap-2.5">
          <div className="h-2.5 w-16 rounded-xs bg-raised" />
          <div className="h-5 w-72 animate-pulse-soft rounded-xs bg-raised" />
          <div className="h-3 w-56 rounded-xs bg-raised/70" />
          <div className="mt-1 flex items-center gap-2 text-sm text-fg-subtle">
            <Spinner size={13} className="text-accent-hover" /> Fetching recommendations from tunebat…
          </div>
        </div>
      </div>
      <div className="h-11 shrink-0 border-b border-line" />
      <div className="h-7 shrink-0 border-b border-line bg-panel" />
      {Array.from({ length: 12 }, (_, i) => (
        <div key={i} className="flex h-[38px] items-center gap-3 border-b border-line/60 px-5" style={{ opacity: 1 - i * 0.07 }}>
          <span className="size-3.5 rounded-xs bg-raised" />
          <span className="size-[22px] rounded-full bg-raised" />
          <span className="size-7 rounded-xs bg-raised" />
          <span className="flex w-[22%] flex-col gap-1.5">
            <span className="h-2.5 w-4/5 animate-pulse-soft rounded-xs bg-raised" />
            <span className="h-2 w-1/2 rounded-xs bg-raised/60" />
          </span>
          <span className="h-[18px] w-8 rounded-xs bg-raised" />
          <span className="h-[18px] w-16 rounded-xs bg-raised/70" />
          <span className="h-[18px] w-8 rounded-xs bg-raised" />
          <span className="flex-1" />
          <span className="h-3 w-28 rounded-xs bg-raised/50" />
        </div>
      ))}
      <div className="flex-1" />
      <div className={cn("flex h-[52px] items-center justify-end border-t border-line bg-panel px-5")}>
        <Button variant="primary" size="md" icon={Download} disabled className="min-w-[188px]">
          Download
        </Button>
      </div>
    </div>
  );
}
