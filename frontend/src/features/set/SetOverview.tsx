/**
 * Set overview: stats, energy lane + BPM lane (two aligned small charts, one axis each), transition-score strip
 * with the weakest transition flagged, and the key path on the Camelot wheel. Hover = crosshair + tooltip; click a
 * track point or a transition bar to select it.
 */
import { useRef, useState } from "react";
import type { CrateSet } from "@/api/types";
import { CamelotWheel } from "@/components/ui";
import { keyColor } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtTime } from "@/lib/format";
import { moveColor } from "@/lib/moves";
import { useWidth } from "@/features/waveform/hooks";
import { Stat } from "./bits";
import { type SetStats, type SetTransition, setTechnique, techniqueShort } from "./model";
import type { SetSelection } from "./SetList";

export interface SetOverviewProps {
  set: CrateSet;
  stats: SetStats;
  selection: SetSelection;
  onSelect: (s: SetSelection) => void;
}

const PAD_L = 50;
const PAD_R = 30;
const E_H = 50; // energy lane
const GAP = 8;
const B_H = 30; // bpm lane
const S_H = 12; // score strip
const X_H = 14; // position labels
const H = E_H + GAP + B_H + GAP + S_H + X_H;

export function SetOverview({ set, stats, selection, onSelect }: SetOverviewProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const width = useWidth(boxRef);
  const [hover, setHover] = useState<number | null>(null);
  const n = set.tracks.length;
  const plotW = Math.max(10, width - PAD_L - PAD_R);
  const x = (i: number) => PAD_L + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const energies = set.tracks.map((t) => Math.round((t.track.energy || 0) * 100));
  const bpms = set.tracks.map((t) => t.track.bpm || 0);
  const bLo = Math.floor(Math.min(...bpms.filter((b) => b > 0), 999) - 1);
  const bHi = Math.ceil(Math.max(...bpms, 0) + 1);
  const yE = (e: number) => E_H - (e / 100) * (E_H - 6) - 3;
  const yB = (b: number) => E_H + GAP + B_H - ((b - bLo) / Math.max(1, bHi - bLo)) * (B_H - 6) - 3;
  const yS = E_H + GAP + B_H + GAP;
  const selId = selection?.kind === "track" ? selection.id : null;
  const selPair = selection?.kind === "transition" ? `${selection.from}>${selection.to}` : null;
  const worstKey = stats.worst ? `${stats.worst.t.from_id}>${stats.worst.t.to_id}` : null;

  const ePath = energies.map((e, i) => `${i ? "L" : "M"}${x(i)},${yE(e)}`).join(" ");
  const bPath = bpms.map((b, i) => `${i ? "L" : "M"}${x(i)},${yB(b)}`).join(" ");
  const keys = set.tracks.map((t) => t.track.camelot);
  const counts: Record<string, number> = {};
  for (const k of keys) if (k) counts[k] = (counts[k] ?? 0) + 1;
  const selTrack = selId ? set.tracks.find((t) => t.track.id === selId) : null;

  const pick = (clientX: number) => {
    const r = boxRef.current?.getBoundingClientRect();
    if (!r || n === 0) return null;
    const px = clientX - r.left;
    return Math.max(0, Math.min(n - 1, Math.round(((px - PAD_L) / plotW) * (n - 1))));
  };
  const hv = hover != null ? set.tracks[hover] : null;

  return (
    <div className="flex shrink-0 items-stretch gap-5 border-b border-line bg-panel/60 px-5 py-3">
      <div className="grid w-[232px] shrink-0 grid-cols-2 content-center gap-x-4 gap-y-2.5">
        <Stat label="Tracks">{n}</Stat>
        <Stat label="Length" sub={stats.mixedS != null ? `≈ ${fmtTime(stats.mixedS)} mixed` : "mix length pending"}>
          {fmtTime(stats.totalS)}
        </Stat>
        <Stat label="Avg transition" sub={stats.scored < set.transitions.length ? `${stats.scored}/${set.transitions.length} scored` : stats.ready < stats.scored ? `${stats.ready}/${stats.scored} with mix points` : "all with mix points"}>
          {stats.avgScore != null ? Math.round(stats.avgScore) : "—"}
        </Stat>
        <Stat
          label="Weakest"
          onClick={stats.worst ? () => onSelect({ kind: "transition", from: stats.worst!.t.from_id, to: stats.worst!.t.to_id }) : undefined}
          sub={stats.worst ? `${set.tracks[stats.worst.index]?.track.name} → ${set.tracks[stats.worst.index + 1]?.track.name}` : undefined}
        >
          {stats.worst ? (
            <span className="inline-flex items-baseline gap-1.5">
              <span className="text-danger">{stats.worst.t.score}</span>
              <span className="text-xs font-medium text-fg-subtle num">
                #{stats.worst.index + 1}→{stats.worst.index + 2}
              </span>
            </span>
          ) : (
            "—"
          )}
        </Stat>
      </div>

      <div ref={boxRef} className="relative min-w-0 flex-1">
        {width > 0 && n > 0 && (
          <svg width={width} height={H} className="block overflow-visible" role="img" aria-label="Energy and BPM across the set">
            {/* energy lane */}
            <text x={0} y={E_H / 2 + 3} className="fill-fg-subtle text-2xs font-semibold tracking-wide">ENERGY</text>
            {[0, 50, 100].map((v) => (
              <g key={v}>
                <line x1={PAD_L} x2={width - PAD_R} y1={yE(v)} y2={yE(v)} stroke="var(--color-line)" strokeWidth={1} />
                <text x={width - PAD_R + 6} y={yE(v) + 3} className="fill-fg-faint text-2xs num">{v}</text>
              </g>
            ))}
            <path d={`${ePath} L${x(n - 1)},${yE(0)} L${x(0)},${yE(0)} Z`} fill="var(--color-accent)" fillOpacity={0.1} />
            <path d={ePath} fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {/* bpm lane */}
            <text x={0} y={E_H + GAP + B_H / 2 + 3} className="fill-fg-subtle text-2xs font-semibold tracking-wide">BPM</text>
            <line x1={PAD_L} x2={width - PAD_R} y1={yB(bHi)} y2={yB(bHi)} stroke="var(--color-line)" />
            <line x1={PAD_L} x2={width - PAD_R} y1={yB(bLo)} y2={yB(bLo)} stroke="var(--color-line)" />
            <text x={width - PAD_R + 6} y={yB(bHi) + 3} className="fill-fg-faint text-2xs num">{bHi}</text>
            <text x={width - PAD_R + 6} y={yB(bLo) + 3} className="fill-fg-faint text-2xs num">{bLo}</text>
            <path d={bPath} fill="none" stroke="var(--color-fg-muted)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            <text x={0} y={yS + 9} className="fill-fg-subtle text-2xs font-semibold tracking-wide">MIXES</text>
            {/* transition score strip */}
            {(set.transitions as SetTransition[]).map((t, i) => {
              const key = `${t.from_id}>${t.to_id}`;
              const x0 = x(i) + 3;
              const x1 = x(i + 1) - 3;
              const worst = key === worstKey;
              const sel = key === selPair;
              return (
                <g key={key} className="cursor-pointer" onClick={() => onSelect({ kind: "transition", from: t.from_id, to: t.to_id })}>
                  <rect x={x0} y={yS - 4} width={Math.max(2, x1 - x0)} height={S_H + 8} fill="transparent" />
                  <rect x={x0} y={yS + 2} width={Math.max(2, x1 - x0)} height={S_H - 4} rx={2} fill={t.pending ? "var(--color-line-strong)" : moveColor(t.key_move)} fillOpacity={t.pending ? 1 : 0.25 + (t.score / 100) * 0.6} />
                  {(worst || sel) && <rect x={x0 - 1.5} y={yS + 0.5} width={Math.max(2, x1 - x0) + 3} height={S_H - 1} rx={3} fill="none" stroke={sel ? "var(--color-accent)" : "var(--color-danger)"} strokeWidth={1.5} />}
                  <title>{`#${i + 1} → #${i + 2}: ${t.pending ? "scoring…" : `score ${t.score}`} · ${t.key_move}${t.technique ? ` · ${techniqueShort(setTechnique(t))}` : ""}`}</title>
                </g>
              );
            })}
            {/* crosshair */}
            {hover != null && <line x1={x(hover)} x2={x(hover)} y1={0} y2={yS - 2} stroke="var(--color-fg-subtle)" strokeWidth={1} />}
            {/* points + labels */}
            {set.tracks.map((st, i) => {
              const c = keyColor(st.track.camelot).base;
              const sel = st.track.id === selId;
              return (
                <g key={st.track.id}>
                  <circle cx={x(i)} cy={yE(energies[i])} r={sel ? 5.5 : 4} fill={c} stroke="var(--color-panel)" strokeWidth={2} />
                  <circle cx={x(i)} cy={yB(bpms[i])} r={sel ? 4.5 : 3} fill="var(--color-fg-muted)" stroke="var(--color-panel)" strokeWidth={2} />
                  <text x={x(i)} y={H - 2} textAnchor="middle" className={cn("text-2xs num", sel ? "fill-accent-hover font-bold" : "fill-fg-subtle")}>{i + 1}</text>
                </g>
              );
            })}
          </svg>
        )}
        {/* hit layer for hover/click on tracks (energy + bpm lanes) */}
        <div
          className="absolute top-0 right-0 left-0 cursor-pointer"
          style={{ height: yS - 2 }}
          onMouseMove={(e) => setHover(pick(e.clientX))}
          onMouseLeave={() => setHover(null)}
          onClick={(e) => {
            const i = pick(e.clientX);
            if (i != null) onSelect({ kind: "track", id: set.tracks[i].track.id });
          }}
        />
        {hv && hover != null && (
          <div
            className="pointer-events-none absolute top-0 z-10 rounded-sm bg-overlay px-2 py-1.5 text-xs shadow-pop"
            style={x(hover) > width - 200 ? { right: width - x(hover) + 10 } : { left: x(hover) + 10 }}
          >
            <div className="max-w-[220px] truncate font-medium text-fg">
              <span className="text-fg-subtle num">{hover + 1}.</span> {hv.track.name}
            </div>
            <div className="mt-0.5 flex items-center gap-2 text-fg-muted num">
              <span className="inline-flex items-center gap-1">
                <span className="size-2 rounded-full" style={{ background: keyColor(hv.track.camelot).base }} />
                {hv.track.camelot || "?"}
              </span>
              <span>Energy {energies[hover]}</span>
              <span>{fmtBpm(hv.track.bpm)} BPM</span>
            </div>
          </div>
        )}
      </div>

      <div className="flex shrink-0 flex-col items-center justify-center">
        <CamelotWheel size={118} path={keys} counts={counts} active={selTrack?.track.camelot ?? null} center={selTrack ? undefined : <tspan className="fill-fg-subtle text-2xs">KEY PATH</tspan>} />
      </div>
    </div>
  );
}
