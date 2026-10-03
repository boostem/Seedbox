/**
 * "Where can this crate go?": the crates its ending mixes into best and the crates whose ending mixes into its
 * start (GET /api/crates/{folder}/links), each one click from a new setlist.
 */
import { ArrowRight, Link2, ListMusic } from "lucide-react";
import { useState } from "react";
import { api } from "@/api/client";
import type { CrateLink } from "@/api/types";
import { Button, CoverArt, EmptyState, Modal, Spinner, Tabs } from "@/components/ui";
import { useAsync } from "@/lib/useAsync";
import { navigate } from "@/lib/router";
import { LinkSummary } from "./LinkCard";

function LinkRow({ link, other, label, onBuild, busy }: { link: CrateLink; other: "to" | "from"; label: string; onBuild: () => void; busy: boolean }) {
  const seed = other === "to" ? link.to_seed : link.from_seed;
  const folder = other === "to" ? link.to_folder : link.from_folder;
  const a = link.from_track;
  const b = link.to_track;
  return (
    <div className="flex items-center gap-3 rounded-md border border-line bg-surface px-3 py-2">
      <CoverArt src={seed?.cover_url} size={38} camelot={seed?.camelot} rounded="sm" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-fg">{seed?.name ?? folder}</div>
        <div className="flex min-w-0 items-center gap-1 truncate text-2xs text-fg-subtle">
          <span className="truncate">{a.name}</span>
          <ArrowRight size={10} className="shrink-0" />
          <span className="truncate">{b.name}</span>
        </div>
        <div className="mt-1">
          <LinkSummary link={link} />
        </div>
      </div>
      <Button size="sm" variant="secondary" icon={ListMusic} loading={busy} onClick={onBuild}>
        {label}
      </Button>
    </div>
  );
}

export function CrateLinksPanel({ folder }: { folder: string }) {
  const q = useAsync((signal) => api.crateLinks(folder, { signal, quiet: true }), [folder]);
  const [tab, setTab] = useState<"out" | "in">("out");
  const [busy, setBusy] = useState<string | null>(null);
  const build = async (crates: string[]) => {
    setBusy(crates.join(">"));
    try {
      const sl = await api.createSetlist(crates);
      navigate({ name: "setlist", id: sl.id });
    } catch {
      /* toasted */
    } finally {
      setBusy(null);
    }
  };
  if (q.loading && !q.data) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-fg-subtle">
        <Spinner /> Scoring this crate against your other crates…
      </div>
    );
  }
  const links = q.data?.[tab] ?? [];
  return (
    <div className="flex flex-col gap-3">
      <Tabs
        variant="segmented"
        value={tab}
        onChange={(v) => setTab(v as "out" | "in")}
        items={[
          { id: "out", label: "This ending mixes into…", count: q.data?.out.length },
          { id: "in", label: "…comes in well after", count: q.data?.in.length },
        ]}
      />
      {links.length === 0 ? (
        <EmptyState compact icon={Link2} title="No other crates yet" description="Build another crate from a new seed, then link them into a long setlist." />
      ) : (
        <div className="flex flex-col gap-2">
          {links.map((l) =>
            tab === "out" ? (
              <LinkRow key={l.to_folder} link={l} other="to" label="Setlist: this → it" busy={busy === `${folder}>${l.to_folder}`} onBuild={() => void build([folder, l.to_folder])} />
            ) : (
              <LinkRow key={l.from_folder} link={l} other="from" label="Setlist: it → this" busy={busy === `${l.from_folder}>${folder}`} onBuild={() => void build([l.from_folder, folder])} />
            ),
          )}
        </div>
      )}
      <p className="text-2xs text-fg-faint">Scored on the last track of one crate and the first track of the other (their set orders). Mix points appear once both are analysed.</p>
    </div>
  );
}

export function CrateLinksButton({ folder, label }: { folder: string; label: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="ghost" icon={Link2} onClick={() => setOpen(true)}>
        Link crates
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} size="lg" title={`Link ${label} with other crates`} description="Chain crates into one long setlist, then render it as one mix.">
        {open && <CrateLinksPanel folder={folder} />}
      </Modal>
    </>
  );
}
