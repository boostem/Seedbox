// Setlists: crates chained back to back into one long set (rendered as one mix).
import { ListMusic, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import type { CrateSummary } from "@/api/types";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Button, Checkbox, CoverArt, EmptyState, Field, Input, KeyBadge, Modal, Page, PageBody, PageHeader, Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtRelative, plural } from "@/lib/format";
import { navigate } from "@/lib/router";
import { useAsync } from "@/lib/useAsync";
import { useApp } from "@/store/app";

function CratePicker({ crates, picked, onToggle }: { crates: CrateSummary[]; picked: string[]; onToggle: (folder: string) => void }) {
  return (
    <div className="flex max-h-[380px] flex-col overflow-y-auto scroll-thin rounded-md border border-line">
      {crates.map((c) => {
        const n = picked.indexOf(c.folder);
        return (
          <label key={c.folder} className={cn("flex h-11 cursor-pointer items-center gap-2.5 border-b border-line px-3 last:border-b-0 hover:bg-raised", n >= 0 && "bg-accent/8")}>
            <Checkbox checked={n >= 0} onChange={() => onToggle(c.folder)} />
            <CoverArt src={c.seed?.cover_url} size={28} camelot={c.seed?.camelot} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm text-fg">{c.seed?.name ?? c.folder}</span>
              <span className="block truncate text-2xs text-fg-subtle">{c.seed ? c.seed.artists.join(", ") : c.folder} · {plural(c.track_count, "track")}</span>
            </span>
            {c.seed && <KeyBadge camelot={c.seed.camelot} size="xs" />}
            {n >= 0 && <span className="grid size-5 place-items-center rounded-full bg-accent text-2xs font-bold text-accent-fg num">{n + 1}</span>}
          </label>
        );
      })}
    </div>
  );
}

export function NewSetlistModal({ open, onClose, initial = [] }: { open: boolean; onClose: () => void; initial?: string[] }) {
  const crates = useApp((s) => s.crates) ?? [];
  const [picked, setPicked] = useState<string[]>(initial);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const toggle = (f: string) => setPicked((p) => (p.includes(f) ? p.filter((x) => x !== f) : [...p, f]));
  const create = async () => {
    setBusy(true);
    try {
      const sl = await api.createSetlist(picked, name.trim() || undefined);
      onClose();
      navigate({ name: "setlist", id: sl.id });
    } catch {
      /* toasted */
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      size="md"
      title="New setlist"
      description="Pick crates in the order you want to play them. You can re-order, auto-order and add more later."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" icon={ListMusic} loading={busy} disabled={!picked.length} onClick={() => void create()}>
            Create{picked.length ? ` with ${plural(picked.length, "crate")}` : ""}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="Name" hint="Optional: defaults to the seeds' names">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Friday warm-up" />
        </Field>
        {crates.length ? <CratePicker crates={crates} picked={picked} onToggle={toggle} /> : <p className="text-sm text-fg-subtle">No crates yet.</p>}
      </div>
    </Modal>
  );
}

export function SetlistsPage() {
  usePageTitle("Setlists");
  const version = useApp((s) => s.setlistsVersion + s.epoch * 1e6);
  const q = useAsync((signal) => api.setlists({ signal, quiet: true }), [version]);
  const [creating, setCreating] = useState(false);
  const list = q.data ?? [];
  const covers = useMemo(() => new Map(list.map((s) => [s.id, s.seeds.filter(Boolean).slice(0, 4)])), [list]);
  return (
    <Page>
      <PageHeader
        title="Setlists"
        subtitle="Crates chained back to back: crate-to-crate transitions, one long set, one continuous mix"
        actions={
          <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
            New setlist
          </Button>
        }
      />
      <PageBody padded>
        {q.loading && !q.data ? (
          <div className="flex items-center gap-2 text-sm text-fg-subtle">
            <Spinner /> Loading…
          </div>
        ) : list.length === 0 ? (
          <EmptyState
            icon={ListMusic}
            title="No setlists yet"
            description="Chain two or more crates into a long set. Setbox scores each crate's ending against the next crate's start and renders the whole thing as one mix."
            action={
              <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
                New setlist
              </Button>
            }
          />
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3">
            {list.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => navigate({ name: "setlist", id: s.id })}
                className="group flex flex-col gap-2.5 rounded-lg border border-line bg-surface p-3 text-left transition-all hover:-translate-y-0.5 hover:border-line-strong hover:shadow-card"
              >
                <div className="grid aspect-[2/1] grid-cols-4 gap-0.5 overflow-hidden rounded-md bg-raised">
                  {(covers.get(s.id) ?? []).map((seed, i) => (
                    <CoverArt key={i} src={seed?.cover_url} fill camelot={seed?.camelot} rounded="xs" />
                  ))}
                </div>
                <div className="min-w-0">
                  <div className="truncate text-md font-semibold text-fg">{s.name}</div>
                  <div className="truncate text-xs text-fg-subtle num">
                    {plural(s.crates.length, "crate")} · {plural(s.track_count, "track")} · {fmtRelative(s.updated_at)}
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </PageBody>
      {creating && <NewSetlistModal open onClose={() => setCreating(false)} />}
    </Page>
  );
}
