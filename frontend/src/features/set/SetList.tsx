/**
 * The running order: sortable track rows (@dnd-kit: drag anywhere on a row, or keyboard via the handle — focus,
 * Space/Enter to lift, ↑/↓ to move, Space/Enter to drop, Esc to cancel) with a transition card between each pair.
 */
import {
  type Announcements,
  closestCenter,
  DndContext,
  type DragEndEvent,
  DragOverlay,
  type DragStartEvent,
  KeyboardSensor,
  type Modifier,
  PointerSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { type HTMLAttributes, type KeyboardEvent, type PointerEvent, type ReactNode, useMemo, useState } from "react";
import type { CrateSet, SetTrack } from "@/api/types";
import { useHotkey } from "@/lib/hotkeys";
import type { SetTransition } from "./model";
import { SetRow } from "./SetRow";
import { TransitionCard } from "./TransitionCard";

export type SetSelection = { kind: "track"; id: string } | { kind: "transition"; from: string; to: string } | null;

export interface SetListProps {
  set: CrateSet;
  selection: SetSelection;
  onSelect: (s: SetSelection) => void;
  onReorder: (ids: string[]) => void;
  expanded: Set<string>;
  onToggleExpanded: (key: string) => void;
  worstKey: string | null;
  auditionKey: string | null;
  onAudition: (t: SetTransition) => void;
}

const vertical: Modifier = ({ transform }) => ({ ...transform, x: 0 });

function SortableItem({ st, children, folder, selected, isSeed, onSelect }: { st: SetTrack; children?: ReactNode; folder: string; selected: boolean; isSeed: boolean; onSelect: () => void }) {
  const { setNodeRef, setActivatorNodeRef, listeners, attributes, transform, transition, isDragging } = useSortable({ id: st.track.id });
  const style = { transform: CSS.Translate.toString(transform), transition };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    (listeners?.onKeyDown as ((e: KeyboardEvent<HTMLDivElement>) => void) | undefined)?.(e);
    // keep the Space/Enter that *lifts* the row away from the global Space = play hotkey. Not while dragging: the
    // KeyboardSensor hears the drop key on `document`, and SetList swallows the page hotkeys during the drag.
    if (!isDragging && (e.code === "Space" || e.code === "Enter") && (e.target as HTMLElement).getAttribute("aria-roledescription") === "sortable") e.stopPropagation();
  };
  return (
    <div ref={setNodeRef} style={style} className="relative" onPointerDown={listeners?.onPointerDown as ((e: PointerEvent<HTMLDivElement>) => void) | undefined} onKeyDown={onKeyDown}>
      <SetRow st={st} folder={folder} selected={selected} isSeed={isSeed} onSelect={onSelect} handleRef={setActivatorNodeRef} handleProps={attributes as HTMLAttributes<HTMLButtonElement>} ghost={isDragging} />
      {children}
    </div>
  );
}

export function SetList({ set, selection, onSelect, onReorder, expanded, onToggleExpanded, worstKey, auditionKey, onAudition }: SetListProps) {
  const ids = useMemo(() => set.tracks.map((t) => t.track.id), [set.tracks]);
  const [active, setActive] = useState<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const byId = useMemo(() => new Map(set.tracks.map((t) => [t.track.id, t])), [set.tracks]);
  const name = (id: string | number) => byId.get(String(id))?.track.display_name ?? "track";
  const pos = (id: string | number | undefined) => (id == null ? 0 : ids.indexOf(String(id)) + 1);

  // while a (keyboard) drag is active, swallow the page/global shortcuts that share its keys
  useHotkey(["ArrowUp", "ArrowDown", "j", "k", "Space", "Enter", "Escape", "Home", "End"], () => true, { enabled: active != null, preventDefault: false });

  const announcements: Announcements = {
    onDragStart: ({ active: a }) => `Picked up ${name(a.id)}, position ${pos(a.id)} of ${ids.length}.`,
    onDragOver: ({ active: a, over }) => (over ? `${name(a.id)} moved to position ${pos(over.id)} of ${ids.length}.` : `${name(a.id)} is no longer over the list.`),
    onDragEnd: ({ active: a, over }) => (over ? `${name(a.id)} dropped at position ${pos(over.id)}.` : `${name(a.id)} dropped.`),
    onDragCancel: ({ active: a }) => `Re-ordering cancelled; ${name(a.id)} is back at position ${pos(a.id)}.`,
  };

  const onStart = (e: DragStartEvent) => {
    setActive(String(e.active.id));
    const st = byId.get(String(e.active.id));
    if (st) onSelect({ kind: "track", id: st.track.id });
  };
  // A keyboard drop/cancel key is still bubbling to the window hotkeys when the drag ends: keep swallowing it
  // (Space would otherwise also start playback) and release the shortcuts on the next task.
  const release = () => setTimeout(() => setActive(null), 0);
  const onEnd = (e: DragEndEvent) => {
    release();
    const { active: a, over } = e;
    if (!over || a.id === over.id) return;
    const from = ids.indexOf(String(a.id));
    const to = ids.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    const next = [...ids];
    next.splice(from, 1);
    next.splice(to, 0, String(a.id));
    onReorder(next);
  };

  const seedId = set.seed_id;
  const activeTrack = active ? byId.get(active) : null;

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} modifiers={[vertical]} onDragStart={onStart} onDragEnd={onEnd} onDragCancel={release} accessibility={{ announcements }}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        <div className="flex flex-col">
          {set.tracks.map((st, i) => {
            const t = set.transitions[i] as SetTransition | undefined;
            const next = set.tracks[i + 1];
            const key = t ? `${t.from_id}>${t.to_id}` : "";
            return (
              <SortableItem
                key={st.track.id}
                st={st}
                folder={set.folder}
                selected={selection?.kind === "track" && selection.id === st.track.id}
                isSeed={st.track.id === seedId}
                onSelect={() => onSelect({ kind: "track", id: st.track.id })}
              >
                {t && next && (
                  <TransitionCard
                    t={t}
                    a={st}
                    b={next}
                    folder={set.folder}
                    dimmed={active != null}
                    selected={selection?.kind === "transition" && selection.from === t.from_id && selection.to === t.to_id}
                    expanded={expanded.has(key)}
                    worst={worstKey === key}
                    auditioning={auditionKey === key}
                    onSelect={() => onSelect({ kind: "transition", from: t.from_id, to: t.to_id })}
                    onToggle={() => onToggleExpanded(key)}
                    onAudition={() => onAudition(t)}
                  />
                )}
              </SortableItem>
            );
          })}
        </div>
      </SortableContext>
      <DragOverlay dropAnimation={{ duration: 160, easing: "cubic-bezier(0.2, 0, 0, 1)" }}>
        {activeTrack ? <SetRow st={activeTrack} folder={set.folder} overlay isSeed={activeTrack.track.id === seedId} /> : null}
      </DragOverlay>
    </DndContext>
  );
}
