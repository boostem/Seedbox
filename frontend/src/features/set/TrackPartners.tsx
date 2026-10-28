/** The track view's "Mix partners" panel: the crate's best next / previous tracks, each insertable next to this one. */
import { ArrowDownToLine, ArrowUpToLine } from "lucide-react";
import { useState } from "react";
import { api } from "@/api/client";
import type { Partner, SetTrack } from "@/api/types";
import { Button, CoverArt, EmptyState, KeyBadge, MoveChip, ScoreChip, Spinner, Tabs } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm } from "@/lib/format";
import { useAsync } from "@/lib/useAsync";
import { useSetRefreshKey } from "@/store/app";
import { techniqueLong, techniqueShort } from "./model";

export interface TrackPartnersProps {
  folder: string;
  st: SetTrack;
  /** Ids currently right after / before this track (to flag partners already in place). */
  nextId: string | null;
  prevId: string | null;
  onPlace: (partnerId: string, where: "after" | "before") => void;
  onSelectTrack: (id: string) => void;
  onPartners?: (ps: Partner[]) => void;
}

export function TrackPartners({ folder, st, nextId, prevId, onPlace, onSelectTrack, onPartners }: TrackPartnersProps) {
  const [dir, setDir] = useState<"out" | "in">("out");
  const refresh = useSetRefreshKey(folder, 800);
  const q = useAsync(
    async (signal) => {
      const ps = await api.partners(folder, st.track.id, dir, { signal, quiet: true });
      onPartners?.(ps);
      return ps;
    },
    [folder, st.track.id, dir, refresh],
  );
  const ps = q.data;
  const adjacent = dir === "out" ? nextId : prevId;
  return (
    <section className="rounded-md border border-line bg-surface">
      <header className="flex items-center gap-3 border-b border-line px-3 py-2">
        <h3 className="label-caps !text-fg-muted">Mix partners</h3>
        <Tabs
          variant="segmented"
          value={dir}
          onChange={setDir}
          items={[
            { id: "out", label: "Best next" },
            { id: "in", label: "Best previous" },
          ]}
        />
        <span className="flex-1" />
        {q.loading && <Spinner size={12} className="text-fg-subtle" />}
        <span className="text-2xs text-fg-subtle">{dir === "out" ? "this track's outro → their intro" : "their outro → this track's intro"}</span>
      </header>
      {q.error && !ps ? (
        <p className="px-3 py-3 text-sm text-danger select-text">{q.error.message}</p>
      ) : ps && ps.length === 0 ? (
        <EmptyState compact title="No partners" description="This crate has no other tracks." />
      ) : (
        <div className={cn("flex flex-col py-1", q.loading && ps && "opacity-70")}>
          {(ps ?? []).map((p) => {
            const t = p.transition;
            const here = p.track.id === adjacent;
            return (
              <div key={p.track.id} className="group flex h-9 items-center gap-2 px-3 hover:bg-hover">
                <ScoreChip value={t.score} move={t.key_move} passed={t.ready} />
                <CoverArt src={p.track.cover_url} size={24} camelot={p.track.camelot} />
                <button type="button" onClick={() => onSelectTrack(p.track.id)} className="min-w-0 flex-1 truncate text-left text-sm hover:underline">
                  <span className="text-fg">{p.track.name}</span> <span className="text-fg-subtle">{p.track.artists.join(", ")}</span>
                </button>
                <KeyBadge camelot={p.track.camelot} keyName={p.track.key} size="xs" />
                <span className="w-8 text-right text-xs text-fg-muted num">{fmtBpm(p.track.bpm)}</span>
                <MoveChip move={t.key_move} size="xs" />
                <span className="hidden w-[92px] truncate text-xs text-fg-subtle xl:inline" title={(t.ready && techniqueLong(t)) || undefined}>
                  {(t.ready && techniqueShort(t)) || "key/BPM only"}
                </span>
                {here ? (
                  <span className="w-[92px] text-center text-2xs font-semibold tracking-wide text-ok uppercase">{dir === "out" ? "already next" : "already before"}</span>
                ) : (
                  <Button
                    size="xs"
                    variant="outline"
                    className="w-[92px]"
                    icon={dir === "out" ? ArrowDownToLine : ArrowUpToLine}
                    onClick={() => onPlace(p.track.id, dir === "out" ? "after" : "before")}
                  >
                    {dir === "out" ? "Insert after" : "Insert before"}
                  </Button>
                )}
              </div>
            );
          })}
          {!ps && (
            <div className="flex h-24 items-center justify-center">
              <Spinner className="text-fg-subtle" />
            </div>
          )}
        </div>
      )}
    </section>
  );
}
