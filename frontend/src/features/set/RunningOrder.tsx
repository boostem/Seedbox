/** The set builder's left column: header, the sortable running order (SetList) and the shortcuts hint. */
import { Keyboard } from "lucide-react";
import { Kbd, Spinner, Tooltip } from "@/components/ui";
import { SetList, type SetListProps } from "./SetList";

export function RunningOrder({ loading, ...list }: SetListProps & { loading: boolean }) {
  const { set } = list;
  const n = set.tracks.length;
  return (
    <div className="flex w-[clamp(400px,40%,600px)] shrink-0 flex-col border-r border-line">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3">
        <span className="label-caps">Running order</span>
        <span className="text-2xs text-fg-subtle num">{n > 1 ? `${set.transitions.length} transitions` : ""}</span>
        <span className="flex-1" />
        {loading && <Spinner size={11} className="text-fg-subtle" />}
      </div>
      <div className="@container min-h-0 flex-1 overflow-auto scroll-thin px-2 py-2">
        <SetList {...list} />
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
  );
}
