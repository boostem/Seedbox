// One setlist: crates back to back (drag to re-order), crate-to-crate links with their mix editor, the full mix.
import { closestCenter, DndContext, type DragEndEvent, KeyboardSensor, type Modifier, PointerSensor, useSensor, useSensors } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { AlertTriangle, ArrowUpRight, GripVertical, ListMusic, Pencil, Plus, Trash2, Wand2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import type { CrateLink, SetlistBlock } from "@/api/types";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Button, CoverArt, EmptyState, IconButton, Input, KeyBadge, Modal, Page, PageHeader, ScoreChip, Select, Spinner, Tooltip } from "@/components/ui";
import { CrateLinksPanel } from "@/features/setlist/CrateLinks";
import { LinkCard } from "@/features/setlist/LinkCard";
import { LinkDetail } from "@/features/setlist/LinkDetail";
import { useSetlist } from "@/features/setlist/useSetlist";
import { SetMixButton, SetMixPanel } from "@/features/setmix/SetMixPanel";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtTime, plural } from "@/lib/format";
import { navigate } from "@/lib/router";
import { useAsync } from "@/lib/useAsync";
import { useApp } from "@/store/app";
import { toast } from "@/store/toasts";

const vertical: Modifier = ({ transform }) => ({ ...transform, x: 0 });
const linkKey = (l: CrateLink) => `${l.from_track.id}>${l.to_track.id}`;

function BlockRow({ b, index, onRemove }: { b: SetlistBlock; index: number; onRemove: () => void }) {
  const { setNodeRef, setActivatorNodeRef, listeners, attributes, transform, transition, isDragging } = useSortable({ id: b.folder });
  const first = b.tracks[0]?.track;
  const last = b.tracks[b.tracks.length - 1]?.track;
  const bpms = b.tracks.map((t) => t.track.bpm).filter((x) => x > 0);
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={cn("group relative flex items-center gap-3 rounded-lg border bg-surface px-2.5 py-2.5 transition-shadow", isDragging ? "z-10 border-accent shadow-pop" : "border-line")}
      {...(listeners ?? {})}
    >
      <button ref={setActivatorNodeRef} {...attributes} type="button" className="cursor-grab text-fg-faint hover:text-fg-muted" aria-label={`Move ${b.seed?.name ?? b.folder}`}>
        <GripVertical size={15} />
      </button>
      <span className="w-4 text-right text-xs text-fg-faint num">{index + 1}</span>
      <CoverArt src={b.seed?.cover_url} size={46} camelot={b.seed?.camelot} rounded="md" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-md font-semibold text-fg">{b.seed?.name ?? b.folder}</span>
          {b.seed && <KeyBadge camelot={b.seed.camelot} size="xs" />}
        </div>
        {b.missing ? (
          <span className="inline-flex items-center gap-1 text-xs text-danger">
            <AlertTriangle size={12} /> Crate folder not found
          </span>
        ) : (
          <>
            <div className="truncate text-xs text-fg-subtle num">
              {plural(b.tracks.length, "track")} · {fmtTime(b.tracks.reduce((s, t) => s + (t.track.duration_s || 0), 0))}
              {bpms.length ? ` · ${fmtBpm(Math.min(...bpms))}–${fmtBpm(Math.max(...bpms))} BPM` : ""}
            </div>
            {first && last && (
              <div className="truncate text-2xs text-fg-faint">
                opens with {first.name} · ends with {last.name}
              </div>
            )}
          </>
        )}
      </div>
      <div className="flex items-center gap-0.5 opacity-60 transition-opacity group-hover:opacity-100" onPointerDown={(e) => e.stopPropagation()}>
        {!b.missing && <IconButton icon={ArrowUpRight} size="sm" label="Open this crate's set" onClick={() => navigate({ name: "set", folder: b.folder })} />}
        <IconButton icon={X} size="sm" label="Remove from the setlist" onClick={onRemove} />
      </div>
    </div>
  );
}

function AddCrate({ setlistCrates, last, onAdd, busy }: { setlistCrates: string[]; last: string | null; onAdd: (folder: string) => void; busy: boolean }) {
  const crates = useApp((s) => s.crates) ?? [];
  const free = crates.filter((c) => !setlistCrates.includes(c.folder));
  const sugg = useAsync(async (signal) => (last ? api.crateLinks(last, { signal, quiet: true }) : null), [last]);
  const best = (sugg.data?.out ?? []).filter((l) => !setlistCrates.includes(l.to_folder)).slice(0, 3);
  if (!free.length) return null;
  return (
    <div className="mt-3 flex flex-col gap-2 rounded-lg border border-dashed border-line-strong px-3 py-2.5">
      <span className="label-caps">Add a crate</span>
      {best.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-2xs text-fg-subtle">Mixes well after the last crate:</span>
          {best.map((l) => (
            <button key={l.to_folder} type="button" disabled={busy} onClick={() => onAdd(l.to_folder)} className="flex h-9 items-center gap-2 rounded-sm px-1.5 text-left hover:bg-raised disabled:opacity-50">
              <CoverArt src={l.to_seed?.cover_url} size={24} camelot={l.to_seed?.camelot} />
              <span className="min-w-0 flex-1 truncate text-sm text-fg">{l.to_seed?.name ?? l.to_folder}</span>
              <ScoreChip value={l.transition.score} move={l.transition.key_move} passed={l.transition.ready} />
              <Plus size={14} className="text-fg-subtle" />
            </button>
          ))}
        </div>
      )}
      <Select value="" disabled={busy} onChange={(e) => e.target.value && onAdd(e.target.value)} aria-label="Add a crate">
        <option value="">Any crate…</option>
        {free.map((c) => (
          <option key={c.folder} value={c.folder}>
            {c.seed?.display_name ?? c.folder}
          </option>
        ))}
      </Select>
    </div>
  );
}

export function SetlistPage({ id }: { id: string }) {
  const s = useSetlist(id);
  const sl = s.setlist;
  usePageTitle("Setlists", sl?.name);
  const [sel, setSel] = useState<string | null>(null); // link key
  const [renaming, setRenaming] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [linksFor, setLinksFor] = useState<string | null>(null);
  const [entryBusy, setEntryBusy] = useState<string | null>(null);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  const folders = useMemo(() => sl?.blocks.map((b) => b.folder) ?? [], [sl]);
  const linkAfter = useMemo(() => new Map((sl?.links ?? []).map((l) => [l.from_folder, l])), [sl]);
  const selLink = sl?.links.find((l) => linkKey(l) === sel) ?? null;
  const trackOf = (folder: string, tid: string) => sl?.blocks.find((b) => b.folder === folder)?.tracks.find((t) => t.track.id === tid);
  const avg = sl?.links.length ? Math.round(sl.links.reduce((n, l) => n + l.transition.score, 0) / sl.links.length) : null;

  if (!sl) {
    return (
      <Page>
        <PageHeader title="Setlist" />
        {s.error ? (
          <EmptyState icon={AlertTriangle} title="Couldn't load this setlist" description={<span className="select-text">{s.error.message}</span>} action={<Button onClick={() => navigate({ name: "setlists" })}>All setlists</Button>} />
        ) : (
          <div className="flex flex-1 items-center justify-center gap-2 text-sm text-fg-subtle">
            <Spinner /> Loading setlist…
          </div>
        )}
      </Page>
    );
  }

  const onEnd = (e: DragEndEvent) => {
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    const next = [...folders];
    const from = next.indexOf(String(active.id));
    const to = next.indexOf(String(over.id));
    next.splice(from, 1);
    next.splice(to, 0, String(active.id));
    void s.reorder(next);
  };

  const useBetterEntry = async (l: CrateLink) => {
    if (!l.better_entry) return;
    const block = sl.blocks.find((b) => b.folder === l.to_folder);
    if (!block) return;
    setEntryBusy(linkKey(l));
    try {
      const ids = [l.better_entry.track.id, ...block.tracks.map((t) => t.track.id).filter((x) => x !== l.better_entry!.track.id)];
      await api.saveOrder(l.to_folder, ids, { quiet: true });
      toast.success(`${l.better_entry.track.name} now opens ${block.seed?.name ?? l.to_folder} · TRCK numbers and playlists rewritten.`, { title: "Crate re-ordered", key: "setlist-entry" });
    } catch (e) {
      toast.error((e as Error).message, { title: "Couldn't re-order that crate", key: "setlist-entry" });
    } finally {
      setEntryBusy(null);
    }
  };

  const doDelete = async () => {
    try {
      await api.deleteSetlist(id);
      navigate({ name: "setlists" });
    } catch {
      /* toasted */
    }
  };

  const seeds = sl.blocks.map((b) => b.seed).filter(Boolean).slice(0, 4);
  return (
    <Page>
      <PageHeader
        leading={
          <div className="grid size-11 grid-cols-2 gap-px overflow-hidden rounded-md bg-raised">
            {seeds.map((sd, i) => (
              <CoverArt key={i} src={sd?.cover_url} fill camelot={sd?.camelot} rounded="xs" />
            ))}
          </div>
        }
        title={
          renaming != null ? (
            <Input
              autoFocus
              value={renaming}
              size="sm"
              onChange={(e) => setRenaming(e.target.value)}
              onBlur={() => {
                if (renaming.trim() && renaming.trim() !== sl.name) void s.rename(renaming.trim());
                setRenaming(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                if (e.key === "Escape") setRenaming(null);
              }}
              className="w-[360px]"
            />
          ) : (
            <button type="button" className="group inline-flex items-center gap-2 text-left" onClick={() => setRenaming(sl.name)} title="Rename">
              {sl.name}
              <Pencil size={13} className="text-fg-faint opacity-0 group-hover:opacity-100" />
            </button>
          )
        }
        subtitle={
          <span className="num">
            {plural(sl.blocks.length, "crate")} · {plural(sl.track_count, "track")} · {fmtTime(sl.duration_s)} of music{avg != null ? ` · links avg ${avg}` : ""}
          </span>
        }
        actions={
          <>
            {s.busy && <Spinner size={12} className="text-fg-subtle" />}
            <Button variant="secondary" icon={Wand2} loading={s.busy === "auto"} disabled={sl.blocks.length < 3} onClick={() => void s.autoOrder()}>
              Auto-order crates
            </Button>
            <SetMixButton target={`setlist:${id}`} title={sl.name} trackCount={sl.track_count} refreshKey={sl.updated_at + sl.track_count} />
            <IconButton icon={Trash2} label="Delete this setlist" onClick={() => setConfirmDelete(true)} />
          </>
        }
      />
      <div className="flex min-h-0 flex-1">
        <div className="flex w-[clamp(420px,42%,640px)] shrink-0 flex-col border-r border-line">
          <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3">
            <span className="label-caps">Crates in play order</span>
            <span className="text-2xs text-fg-subtle">drag to re-order · click a link to edit its transition</span>
          </div>
          <div className="min-h-0 flex-1 overflow-auto scroll-thin px-3 py-3">
            {sl.blocks.length === 0 ? (
              <EmptyState compact icon={ListMusic} title="No crates in this setlist" />
            ) : (
              <DndContext sensors={sensors} collisionDetection={closestCenter} modifiers={[vertical]} onDragEnd={onEnd}>
                <SortableContext items={folders} strategy={verticalListSortingStrategy}>
                  {sl.blocks.map((b, i) => {
                    const l = linkAfter.get(b.folder);
                    return (
                      <div key={b.folder} className="animate-rise-in" style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}>
                        <BlockRow b={b} index={i} onRemove={() => void s.setCrates(folders.filter((f) => f !== b.folder))} />
                        {l && i + 1 < sl.blocks.length && (
                          <LinkCard link={l} selected={sel === linkKey(l)} onSelect={() => setSel(linkKey(l))} onUseBetterEntry={() => void useBetterEntry(l)} busy={entryBusy === linkKey(l)} />
                        )}
                        {!l && i + 1 < sl.blocks.length && <div className="my-1.5 ml-[11px] h-4 w-px bg-line" />}
                      </div>
                    );
                  })}
                </SortableContext>
              </DndContext>
            )}
            <AddCrate setlistCrates={folders} last={folders[folders.length - 1] ?? null} busy={s.busy === "crates"} onAdd={(f) => void s.setCrates([...folders, f])} />
          </div>
        </div>
        <div className="min-w-0 flex-1 overflow-auto scroll-thin">
          {selLink && trackOf(selLink.from_folder, selLink.from_track.id) && trackOf(selLink.to_folder, selLink.to_track.id) ? (
            <LinkDetail key={linkKey(selLink)} setlistId={id} link={selLink} a={trackOf(selLink.from_folder, selLink.from_track.id)!} b={trackOf(selLink.to_folder, selLink.to_track.id)!} />
          ) : (
            <div className="flex flex-col gap-5 px-5 py-4">
              <section className="flex flex-col gap-2">
                <h2 className="label-caps !text-fg-muted">The whole setlist as one mix</h2>
                <SetMixPanel target={`setlist:${id}`} trackCount={sl.track_count} refreshKey={sl.updated_at + sl.track_count} />
              </section>
              <section className="flex flex-col gap-2">
                <h2 className="label-caps !text-fg-muted">Crate links</h2>
                {sl.links.length ? (
                  <div className="flex flex-wrap gap-2">
                    {sl.links.map((l) => (
                      <button key={linkKey(l)} type="button" onClick={() => setSel(linkKey(l))} className="flex items-center gap-2 rounded-md border border-line bg-surface px-2.5 py-1.5 text-left text-xs hover:border-line-strong">
                        <ScoreChip value={l.transition.score} move={l.transition.key_move} passed={l.transition.ready} />
                        <span className="truncate text-fg">
                          {l.from_seed?.name ?? l.from_folder} → {l.to_seed?.name ?? l.to_folder}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-fg-subtle">Add a second crate to get a link.</p>
                )}
                {folders.length > 0 && (
                  <Tooltip content="See every crate this one mixes into">
                    <span className="self-start">
                      <Button size="sm" variant="ghost" onClick={() => setLinksFor(folders[folders.length - 1])}>
                        Explore links from the last crate
                      </Button>
                    </span>
                  </Tooltip>
                )}
              </section>
            </div>
          )}
        </div>
      </div>
      <Modal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        size="sm"
        title="Delete this setlist?"
        description="The crates, their tracks and any rendered mixes stay where they are."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Keep it
            </Button>
            <Button variant="danger" icon={Trash2} onClick={() => void doDelete()}>
              Delete
            </Button>
          </>
        }
      />
      <Modal open={linksFor != null} onClose={() => setLinksFor(null)} size="lg" title="Crate links">
        {linksFor && <CrateLinksPanel folder={linksFor} />}
      </Modal>
    </Page>
  );
}
