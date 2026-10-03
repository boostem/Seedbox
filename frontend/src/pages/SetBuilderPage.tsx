// OWNER: FRONTEND-SET agent. Set builder: running order + transitions (left), track / mix detail (right), overview.
import { Activity, AlertTriangle, AudioLines, AudioWaveform, Check, FolderOpen, Keyboard, Search, Sprout, Undo2, Wand2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Button, CoverArt, EmptyState, IconButton, Kbd, Page, PageHeader, Spinner, Tooltip } from "@/components/ui";
import { audition, MixDetail, requestPlay, type SetSelection, SetList, SetOverview, setStats, TrackDetail, useAudition, useSetModel } from "@/features/set";
import { CrateLinksButton } from "@/features/setlist/CrateLinks";
import { SetMixButton } from "@/features/setmix/SetMixPanel";
import { cn } from "@/lib/cn";
import { fmtTime, plural } from "@/lib/format";
import { useHotkey } from "@/lib/hotkeys";
import { navigate, useHashQuery } from "@/lib/router";
import { useListNav } from "@/lib/useListNav";
import { useApp, useStemsSummary } from "@/store/app";
import { player, usePlayer } from "@/audio/player";

const OVERVIEW_KEY = "seedbox.set.overview";

function loadPref(): boolean {
  try {
    return localStorage.getItem(OVERVIEW_KEY) === "1";
  } catch {
    return true;
  }
}

const selKey = (s: SetSelection) => (!s ? null : s.kind === "track" ? `t:${s.id}` : `x:${s.from}>${s.to}`);

export function SetBuilderPage({ folder }: { folder: string }) {
  usePageTitle("Crates", folder);
  const m = useSetModel(folder);
  const set = m.set;
  const [selection, setSelection] = useState<SetSelection>(null);
  const query = useHashQuery();
  const [initial] = useState(() => ({ t: Number(query.get("t")), x: Number(query.get("x")) })); // deep link: ?t=<n> track, ?x=<n> transition (1-based)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [showOverview, setShowOverview] = useState(loadPref);
  const roughKey = useAudition((s) => (s.status !== "idle" ? s.key : null));
  // the rendered transition plays through the global player as "mix:<from>><to>"
  const renderKey = usePlayer((s) => (s.currentId?.startsWith("mix:") && (s.status === "playing" || s.status === "loading") ? s.currentId.slice(4) : null));
  const auditionKey = roughKey ?? renderKey;
  const stats = useMemo(() => (set ? setStats(set) : null), [set]);
  const seed = set?.tracks.find((t) => t.track.id === set.seed_id)?.track ?? null;
  const worstKey = stats?.worst && set && set.transitions.length > 1 ? `${stats.worst.t.from_id}>${stats.worst.t.to_id}` : null;

  // stop an audition when leaving the page
  useEffect(() => () => audition.stop(), []);

  // default / repair selection when the set changes
  useEffect(() => {
    if (!set?.tracks.length) return;
    setSelection((s) => {
      if (s?.kind === "track" && set.tracks.some((t) => t.track.id === s.id)) return s;
      if (s?.kind === "transition") {
        if (set.transitions.some((t) => t.from_id === s.from && t.to_id === s.to)) return s;
        const t = set.transitions.find((x) => x.from_id === s.from);
        return t ? { kind: "transition", from: t.from_id, to: t.to_id } : { kind: "track", id: s.from };
      }
      const x = set.transitions[initial.x - 1];
      if (x) return { kind: "transition", from: x.from_id, to: x.to_id };
      return { kind: "track", id: (set.tracks[initial.t - 1] ?? set.tracks[0]).track.id };
    });
  }, [set, initial]);

  // Space plays the selected track (global hotkey) — keep the store selection in sync
  const selTrack = selection?.kind === "track" ? set?.tracks.find((t) => t.track.id === selection.id) : undefined;
  useEffect(() => {
    if (selTrack) useApp.getState().select({ kind: "file", track: selTrack.track, url: selTrack.audio_url });
  }, [selTrack]);

  const select = useCallback((s: SetSelection) => setSelection(s), []);
  const navIds = useMemo(() => {
    if (!set) return [];
    const out: string[] = [];
    set.tracks.forEach((t, i) => {
      out.push(`t:${t.track.id}`);
      const x = set.transitions[i];
      if (x && i + 1 < set.tracks.length) out.push(`x:${x.from_id}>${x.to_id}`);
    });
    return out;
  }, [set]);
  useListNav(navIds, selKey(selection), (id) => {
    if (id.startsWith("t:")) setSelection({ kind: "track", id: id.slice(2) });
    else {
      const [from, to] = id.slice(2).split(">");
      setSelection({ kind: "transition", from, to });
    }
  });

  const tIndex = selection?.kind === "transition" && set ? set.transitions.findIndex((t) => t.from_id === selection.from && t.to_id === selection.to) : -1;
  const selTransition = tIndex >= 0 && set ? set.transitions[tIndex] : null;

  // play a transition: open its mix view, which renders + plays it (or falls back to the rough audition)
  const doAudition = useCallback(
    (key: string) => {
      if (!set) return;
      const t = set.transitions.find((x) => `${x.from_id}>${x.to_id}` === key);
      if (!t) return;
      if (audition.isActive(key)) return audition.stop();
      if (usePlayer.getState().currentId === `mix:${key}`) return player.stop();
      setSelection({ kind: "transition", from: t.from_id, to: t.to_id });
      requestPlay(key);
    },
    [set],
  );

  useHotkey("Alt+ArrowUp", () => selection?.kind === "track" && m.moveBy(selection.id, -1), { enabled: selection?.kind === "track" });
  useHotkey("Alt+ArrowDown", () => selection?.kind === "track" && m.moveBy(selection.id, 1), { enabled: selection?.kind === "track" });
  useHotkey("Mod+z", () => m.undo(), { enabled: m.canUndo });
  useHotkey("Space", () => {
    if (selection?.kind !== "transition" || !selTransition?.ready) return false;
    doAudition(`${selTransition.from_id}>${selTransition.to_id}`);
  });
  useHotkey("Enter", () => {
    if (selection?.kind !== "transition") return false;
    const k = `${selection.from}>${selection.to}`;
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
  });
  useHotkey("Escape", () => (roughKey ? audition.stop() : renderKey ? player.stop() : false), { enabled: !!auditionKey });

  const toggleOverview = () => {
    setShowOverview((v) => {
      try {
        localStorage.setItem(OVERVIEW_KEY, v ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !v;
    });
  };

  // ---- states without a set
  if (!set) {
    return (
      <Page>
        <PageHeader title={folder} subtitle="Set builder" />
        {m.error ? (
          <EmptyState
            icon={AlertTriangle}
            title={(m.error as { status?: number }).status === 404 ? "Crate not found" : "Couldn't load this set"}
            description={<span className="select-text">{m.error.message}</span>}
            action={
              <>
                <Button onClick={m.reload}>Retry</Button>
                <Button variant="ghost" onClick={() => navigate({ name: "crates" })}>
                  All crates
                </Button>
              </>
            }
          />
        ) : (
          <div className="flex flex-1 items-center justify-center gap-2 text-sm text-fg-subtle">
            <Spinner /> Loading set…
          </div>
        )}
      </Page>
    );
  }

  const n = set.tracks.length;
  const saveLabel =
    m.save === "saving" ? (
      <span className="inline-flex items-center gap-1 text-fg-subtle">
        <Spinner size={11} /> Saving order…
      </span>
    ) : m.save === "saved" ? (
      <span className="inline-flex items-center gap-1 text-ok">
        <Check size={12} /> Order saved · TRCK, .m3u8, .xml updated
      </span>
    ) : m.save === "error" ? (
      <span className="inline-flex items-center gap-1 text-warn">
        <AlertTriangle size={12} /> Last move not saved · order reverted
      </span>
    ) : null;

  return (
    <Page>
      <PageHeader
        leading={<CoverArt src={seed?.cover_url} size={44} camelot={seed?.camelot} rounded="md" />}
        title={seed?.name ?? set.folder}
        subtitle={
          <span className="flex items-center gap-2">
            <span>{seed ? seed.artists.join(", ") : "Set"}</span>
            <span className="text-fg-faint">·</span>
            <span className="num">{plural(n, "track")}</span>
            {stats && <span className="num">· {fmtTime(stats.totalS)}</span>}
            {n > 0 && <StemsChip folder={set.folder} ids={set.tracks.map((t) => t.track.id)} />}
          </span>
        }
        actions={
          <>
            <span className="mr-1 text-xs">{saveLabel}</span>
            <Button variant="ghost" icon={Undo2} kbd="Mod+Z" disabled={!m.canUndo} onClick={m.undo}>
              Undo
            </Button>
            <Button variant="secondary" icon={Wand2} loading={m.autoOrdering} disabled={n < 3} onClick={() => void m.autoOrder()}>
              Auto-order
            </Button>
            <SetMixButton target={`crate:${set.folder}`} title={seed?.name ?? set.folder} trackCount={n} refreshKey={set.tracks.map((t) => t.track.id).join()} />
            <CrateLinksButton folder={set.folder} label={seed?.name ?? set.folder} />
            <IconButton icon={Activity} label={showOverview ? "Hide set overview" : "Show set overview"} active={showOverview} onClick={toggleOverview} />
            <IconButton icon={FolderOpen} label="Reveal crate in Finder" onClick={() => void api.reveal(set.path)} />
            {set.seed_id && <IconButton icon={Sprout} label="More recommendations for this seed" onClick={() => navigate({ name: "crate", seedId: set.seed_id! })} />}
          </>
        }
      />
      {n === 0 ? (
        <EmptyState
          icon={AudioWaveform}
          title="This crate has no tracks yet"
          description="Download some recommendations into it, then build the set here."
          action={
            <Button variant="primary" icon={Search} onClick={() => (set.seed_id ? navigate({ name: "crate", seedId: set.seed_id }) : navigate({ name: "search" }))}>
              {set.seed_id ? "Open recommendations" : "Search"}
            </Button>
          }
        />
      ) : (
        <>
          {showOverview && stats && n > 1 && <SetOverview set={set} stats={stats} selection={selection} onSelect={select} />}
          <div className="flex min-h-0 flex-1">
            <div className="flex w-[clamp(400px,40%,600px)] shrink-0 flex-col border-r border-line">
              <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3">
                <span className="label-caps">Running order</span>
                <span className="text-2xs text-fg-subtle num">{n > 1 ? `${set.transitions.length} transitions` : ""}</span>
                <span className="flex-1" />
                {m.loading && <Spinner size={11} className="text-fg-subtle" />}
              </div>
              <div className="@container min-h-0 flex-1 overflow-auto scroll-thin px-2 py-2">
                <SetList
                  set={set}
                  selection={selection}
                  onSelect={select}
                  onReorder={(ids) => void m.reorder(ids)}
                  expanded={expanded}
                  onToggleExpanded={(k) =>
                    setExpanded((s) => {
                      const nx = new Set(s);
                      if (nx.has(k)) nx.delete(k);
                      else nx.add(k);
                      return nx;
                    })
                  }
                  worstKey={worstKey}
                  auditionKey={auditionKey}
                  onAudition={(t) => doAudition(`${t.from_id}>${t.to_id}`)}
                />
              </div>
              <div className="flex h-7 shrink-0 items-center justify-end border-t border-line px-3 text-2xs text-fg-faint">
                <Tooltip
                  side="top"
                  content={
                    <span className="flex flex-col gap-1 py-0.5 text-left">
                      <span><Kbd combo="ArrowUp" subtle /> <Kbd combo="ArrowDown" subtle /> select</span>
                      <span><Kbd combo="Alt+ArrowUp" subtle /> <Kbd combo="Alt+ArrowDown" subtle /> move</span>
                      <span>drag to re-order</span>
                      <span><Kbd combo="Space" subtle /> play / audition</span>
                      <span><Kbd combo="Enter" subtle /> notes</span>
                    </span>
                  }
                >
                  <span className="inline-flex items-center gap-1 hover:text-fg-muted">
                    <Keyboard size={12} /> shortcuts
                  </span>
                </Tooltip>
              </div>
            </div>
            <div className={cn("min-w-0 flex-1 overflow-auto scroll-thin")}>
              {selection?.kind === "transition" && selTransition && tIndex >= 0 ? (
                <MixDetail
                  key={`${selTransition.from_id}>${selTransition.to_id}`}
                  folder={folder}
                  t={selTransition}
                  a={set.tracks[tIndex]}
                  b={set.tracks[tIndex + 1]}
                  index={tIndex}
                  count={set.transitions.length}
                  onPrev={tIndex > 0 ? () => select({ kind: "transition", from: set.transitions[tIndex - 1].from_id, to: set.transitions[tIndex - 1].to_id }) : undefined}
                  onNext={tIndex + 1 < set.transitions.length ? () => select({ kind: "transition", from: set.transitions[tIndex + 1].from_id, to: set.transitions[tIndex + 1].to_id }) : undefined}
                  onSelectTrack={(id) => select({ kind: "track", id })}
                />
              ) : selTrack ? (
                <TrackDetail
                  key={selTrack.track.id}
                  folder={folder}
                  st={selTrack}
                  index={set.tracks.indexOf(selTrack)}
                  total={n}
                  isSeed={selTrack.track.id === set.seed_id}
                  nextId={set.tracks[set.tracks.indexOf(selTrack) + 1]?.track.id ?? null}
                  prevId={set.tracks[set.tracks.indexOf(selTrack) - 1]?.track.id ?? null}
                  onPlace={(pid, where) => m.placeNextTo(pid, selTrack.track.id, where)}
                  onSelectTrack={(id) => select({ kind: "track", id })}
                  onPartners={(ps) => m.rememberTransitions(ps.map((p) => p.transition))}
                />
              ) : (
                <EmptyState compact title="Select a track or a transition" />
              )}
            </div>
          </div>
        </>
      )}
    </Page>
  );
}

/** "Vocals split 5/12" in the header while the crate's vocal stems are being split in the background. */
function StemsChip({ folder, ids }: { folder: string; ids: string[] }) {
  const st = useStemsSummary(folder, ids);
  if (!st.available) {
    return (
      <Tooltip content={st.reason ?? "Vocal splitting isn't available on this install."}>
        <span className="flex items-center gap-1 text-fg-faint">
          · <AudioLines size={11} /> no vocal stems
        </span>
      </Tooltip>
    );
  }
  const done = st.ready >= st.total;
  if (st.waiting && !done) {
    return (
      <Tooltip content={st.waiting}>
        <span className="flex items-center gap-1 text-warn">
          · <AudioLines size={11} /> vocal model: retrying
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip content={done ? "Every song's vocals are split: transitions hand the vocals over cleanly." : "Each song's vocals are split in the background, songs you're using first. Transitions use them as they land."}>
      <span className={cn("flex items-center gap-1 num", done ? "text-ok" : "text-fg-subtle")}>
        · <AudioLines size={11} /> {done ? "vocals split" : `vocals ${st.ready}/${st.total}${st.running ? ` · ${Math.round(st.running.progress * 100)}%` : ""}`}
      </span>
    </Tooltip>
  );
}
