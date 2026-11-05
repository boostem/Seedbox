import { Pin, TriangleAlert } from "lucide-react";
import { Fragment } from "react";
import type { SetOrderResult, SetTransition } from "@/api/types";
import { IconButton, KeyBadge } from "@/components/ui";
import { cn } from "@/lib/cn";

const WARN: SetTransition["flags"] = ["key_clash", "tempo_over_limit"];

export interface OrderListProps {
  result: SetOrderResult;
  /** Positions pinned right now. */
  pinned: ReadonlySet<number>;
  onPin: (position: number) => void;
}

/** The running order, with why each track follows the one before. */
export function OrderList({ result, pinned, onPin }: OrderListProps) {
  return (
    <ol className="flex flex-col">
      {result.order.map((t, i) => {
        const next = result.transitions[i];
        const warn = next?.flags.some((f) => WARN.includes(f));
        const on = pinned.has(t.position);
        return (
          <Fragment key={`${t.position}-${t.hash}`}>
            <li className="flex h-control-xl items-center gap-3 px-3">
              <span className="num w-6 shrink-0 text-right text-xs text-fg-subtle">{t.position + 1}</span>
              <IconButton
                size="sm"
                icon={Pin}
                active={on}
                label={on ? `Unpin ${t.title}` : `Pin ${t.title} at ${t.position + 1}`}
                tooltip={on ? "Pinned here: ordering won't move it" : "Pin it here"}
                className={cn(on && "text-accent")}
                onClick={() => onPin(t.position)}
              />
              <div className="min-w-0 flex-1 truncate text-sm">
                <span className="text-fg">{t.title}</span>
                {t.artist && <span className="text-fg-subtle"> · {t.artist}</span>}
              </div>
              {t.shift !== 0 && <span className="num shrink-0 text-xs text-fg-muted">{t.shift > 0 ? "+1" : "-1"} st</span>}
              <KeyBadge camelot={t.key} size="xs" className="shrink-0" />
              <span className="num w-10 shrink-0 text-right text-xs text-fg-muted">{t.bpm === null ? "–" : t.bpm.toFixed(0)}</span>
              <span className="num w-6 shrink-0 text-right text-xs text-fg-muted" title="Energy, 1 to 10">
                {t.rating ?? "–"}
              </span>
            </li>
            {next && (
              <li className={cn("flex items-center gap-1.5 border-b border-line/60 pb-1 pl-20 pr-3 text-xs", warn ? "text-warn" : "text-fg-subtle")}>
                {warn && <TriangleAlert size={12} aria-label="Check this one" className="shrink-0" />}
                <span className="truncate">{next.text}</span>
              </li>
            )}
          </Fragment>
        );
      })}
    </ol>
  );
}
