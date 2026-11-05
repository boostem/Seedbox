import { Pin, TriangleAlert, Waypoints } from "lucide-react";
import { Fragment, type ReactNode } from "react";
import type { SetOrderTrack, SetTransition } from "@/api/types";
import { Button, IconButton, KeyBadge } from "@/components/ui";
import { cn } from "@/lib/cn";

const WARN: SetTransition["flags"] = ["key_clash", "tempo_over_limit"];

export interface OrderListProps {
  result: { order: SetOrderTrack[]; transitions: SetTransition[] };
  /** Positions pinned right now. */
  pinned?: ReadonlySet<number>;
  /** Without it, tracks can't be pinned. */
  onPin?: (position: number) => void;
  /** Offered on each gap; without it, as on an order that isn't saved yet, gaps only show their score. */
  onBridge?: (position: number) => void;
  /** The transition whose bridge search is open, and that search. */
  bridgeAt?: number | null;
  bridgePanel?: ReactNode;
  /** A small label on a track: the crate it's from. */
  label?: (track: SetOrderTrack) => string | undefined;
}

/** The running order, with why each track follows the one before. */
export function OrderList({ result, pinned, onPin, onBridge, bridgeAt, bridgePanel, label }: OrderListProps) {
  return (
    <ol className="flex flex-col">
      {result.order.map((t, i) => {
        const next = result.transitions[i];
        const warn = next?.gap || next?.flags.some((f) => WARN.includes(f));
        const on = pinned?.has(t.position) ?? false;
        const from = label?.(t);
        return (
          <Fragment key={`${t.position}-${t.hash}`}>
            <li className="flex h-control-xl items-center gap-3 px-3">
              <span className="num w-6 shrink-0 text-right text-xs text-fg-subtle">{t.position + 1}</span>
              {onPin && (
                <IconButton
                  size="sm"
                  icon={Pin}
                  active={on}
                  label={on ? `Unpin ${t.title}` : `Pin ${t.title} at ${t.position + 1}`}
                  tooltip={on ? "Pinned here: ordering won't move it" : "Pin it here"}
                  className={cn(on && "text-accent")}
                  onClick={() => onPin(t.position)}
                />
              )}
              <div className="min-w-0 flex-1 truncate text-sm">
                <span className="text-fg">{t.title}</span>
                {t.artist && <span className="text-fg-subtle"> · {t.artist}</span>}
              </div>
              {from && <span className="max-w-32 shrink-0 truncate text-xs text-fg-subtle">{from}</span>}
              {t.shift !== 0 && <span className="num shrink-0 text-xs text-fg-muted">{t.shift > 0 ? "+1" : "-1"} st</span>}
              <KeyBadge camelot={t.key} size="xs" className="shrink-0" />
              <span className="num w-10 shrink-0 text-right text-xs text-fg-muted">{t.bpm === null ? "–" : t.bpm.toFixed(0)}</span>
              <span className="num w-6 shrink-0 text-right text-xs text-fg-muted" title="Energy, 1 to 10">
                {t.rating ?? "–"}
              </span>
            </li>
            {next && (
              <li className={cn("flex items-center gap-1.5 border-b border-line/60 pb-1 pr-3 text-xs", onPin ? "pl-20" : "pl-12", warn ? "text-warn" : "text-fg-subtle")}>
                {warn && <TriangleAlert size={12} aria-label="Check this one" className="shrink-0" />}
                <span className="min-w-0 flex-1 truncate">{next.text}</span>
                {next.gap && (
                  <span className="num shrink-0" title="How well these two mix, out of 100">
                    {next.score}
                  </span>
                )}
                {next.gap && onBridge && (
                  <Button size="sm" variant="ghost" icon={Waypoints} className="shrink-0" onClick={() => onBridge(t.position)}>
                    Find a bridge
                  </Button>
                )}
              </li>
            )}
            {next && bridgeAt === t.position && <li className="border-b border-line/60">{bridgePanel}</li>}
          </Fragment>
        );
      })}
    </ol>
  );
}
