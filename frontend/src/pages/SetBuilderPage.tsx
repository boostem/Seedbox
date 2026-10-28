// OWNER: FRONTEND-SET agent. Set builder: running order + transitions (left), track / mix detail (right), overview.
import { Activity, AlertTriangle, AudioWaveform, FolderOpen, Search, Sprout, Undo2, Wand2 } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Button, CoverArt, EmptyState, IconButton, Page, PageHeader, Spinner } from "@/components/ui";
import { MixDetail, RunningOrder, SaveStatus, SetOverview, setStats, TrackDetail, useSetModel, useSetSelection } from "@/features/set";
import { cn } from "@/lib/cn";
import { fmtTime, plural } from "@/lib/format";
import { navigate } from "@/lib/router";

const OVERVIEW_KEY = "seedbox.set.overview";

// shown unless the user has hidden it
function loadPref(): boolean {
  try {
    return localStorage.getItem(OVERVIEW_KEY) !== "0";
  } catch {
    return true;
  }
}

export function SetBuilderPage({ folder }: { folder: string }) {
  usePageTitle("Crates", folder);
  const m = useSetModel(folder);
  const set = m.set;
  const { selection, select, selTrack, selTransition, tIndex, expanded, toggleExpanded, auditionKey, doAudition } = useSetSelection(m);
  const [showOverview, setShowOverview] = useState(loadPref);
  const stats = useMemo(() => (set ? setStats(set) : null), [set]);
  const seed = set?.tracks.find((t) => t.track.id === set.seed_id)?.track ?? null;
  const worstKey = stats?.worst && set && set.transitions.length > 1 ? `${stats.worst.t.from_id}>${stats.worst.t.to_id}` : null;

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
          </span>
        }
        actions={
          <>
            <span className="mr-1 text-xs">
              <SaveStatus save={m.save} />
            </span>
            <Button variant="ghost" icon={Undo2} kbd="Mod+Z" disabled={!m.canUndo} onClick={m.undo}>
              Undo
            </Button>
            <Button variant="secondary" icon={Wand2} loading={m.autoOrdering} disabled={n < 3} onClick={() => void m.autoOrder()}>
              Auto-order
            </Button>
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
            <RunningOrder
              set={set}
              loading={m.loading}
              selection={selection}
              onSelect={select}
              onReorder={(ids) => void m.reorder(ids)}
              expanded={expanded}
              onToggleExpanded={toggleExpanded}
              worstKey={worstKey}
              auditionKey={auditionKey}
              onAudition={(t) => doAudition(`${t.from_id}>${t.to_id}`)}
            />
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
