/** The mix editor for a setlist's crate-to-crate link (plans saved under the setlist, rendered like any pair). */
import { ArrowRight, Info } from "lucide-react";
import type { CrateLink, SetTrack } from "@/api/types";
import { Button, CoverArt } from "@/components/ui";
import { cn } from "@/lib/cn";
import { MixEditor, useTrackAnalysis } from "@/features/set";
import { useMixPlan } from "@/features/set/mix/useMixPlan";
import { useSessionState } from "@/features/set/useSessionState";
import { WavePlaceholder } from "@/features/set/TrackDetail";
import { LinkSummary } from "./LinkCard";

function Deck({ side, st, crate }: { side: "A" | "B"; st: SetTrack; crate: string }) {
  return (
    <div className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-2 py-1.5">
      <span className={cn("grid size-6 shrink-0 place-items-center rounded-xs text-xs font-bold", side === "A" ? "bg-raised text-fg" : "bg-accent/20 text-accent-hover")}>{side}</span>
      <CoverArt src={st.track.cover_url} size={32} camelot={st.track.camelot} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{st.track.name}</div>
        <div className="truncate text-xs text-fg-subtle">
          {st.track.artists.join(", ")} · <span className="text-fg-faint">{crate}</span>
        </div>
      </div>
    </div>
  );
}

export function LinkDetail({ setlistId, link, a, b }: { setlistId: string; link: CrateLink; a: SetTrack; b: SetTrack }) {
  const scope = `setlist:${setlistId}`;
  const va = useTrackAnalysis(link.from_folder, a.track.id, a.analysis_state);
  const vb = useTrackAnalysis(link.to_folder, b.track.id, b.analysis_state);
  const anA = va.status === "ready" ? va.analysis : null;
  const anB = vb.status === "ready" ? vb.analysis : null;
  const ready = !!anA && !!anB;
  const mp = useMixPlan(scope, a.track.id, b.track.id, ready);
  const [details, setDetails] = useSessionState("seedbox.mix.details", false);
  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      <span className="label-caps">Crate link</span>
      <div className="flex items-center gap-2">
        <Deck side="A" st={a} crate={link.from_seed?.name ?? link.from_folder} />
        <ArrowRight size={16} className="shrink-0 text-fg-subtle" />
        <Deck side="B" st={b} crate={link.to_seed?.name ?? link.to_folder} />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <LinkSummary link={link} />
        <span className="flex-1" />
        <Button variant="ghost" size="sm" icon={Info} aria-pressed={details} className={cn(details && "bg-raised text-fg")} onClick={() => setDetails((v) => !v)}>
          Details
        </Button>
      </div>
      {mp.plan ? (
        <MixEditor key={`${a.track.id}>${b.track.id}`} folder={scope} a={a} b={b} anA={anA} anB={anB} mp={{ ...mp, plan: mp.plan }} details={details} />
      ) : mp.error ? (
        <p className="text-xs text-fg-subtle select-text">Mix editor unavailable for this link: {mp.error.message}</p>
      ) : (
        <WavePlaceholder view={!ready ? (va.status !== "ready" ? va : vb) : { status: "loading" }} height={220} />
      )}
      {details && (
        <ul className="flex flex-col gap-1.5 rounded-md border border-line bg-surface px-3 py-2.5 text-sm select-text">
          {[...link.transition.notes, ...link.transition.warnings.map((w) => `⚠ ${w}`)].map((n, i) => (
            <li key={i} className={cn(i === 0 ? "font-medium text-fg" : "text-fg-muted")}>
              {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
