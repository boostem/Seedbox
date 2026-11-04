import { AlertTriangle, Check, Download, RotateCw, Search, SearchX } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { TunebatDownload, TunebatTrack } from "@/api/types";
import { Button, CoverArt, EmptyState, Input, KeyBadge, Skeleton, Spinner } from "@/components/ui";
import { fmtTime } from "@/lib/format";

type Search = { state: "idle" } | { state: "searching" } | { state: "done"; term: string; items: TunebatTrack[] } | { state: "failed"; error: string };

const WORD: Partial<Record<TunebatDownload["state"], string>> = { queued: "Waiting…", finding: "Finding…", downloading: "Downloading…" };

function DownloadAction({ dl, onStart }: { dl: TunebatDownload | undefined; onStart: () => void }) {
  if (dl?.state === "done") {
    return (
      <span className="flex items-center gap-1 text-xs text-ok">
        <Check size={14} /> In your library
      </span>
    );
  }
  if (dl && dl.state !== "failed") {
    return (
      <span className="flex items-center gap-1.5 text-xs text-fg-muted">
        <Spinner size={13} /> {WORD[dl.state]}
      </span>
    );
  }
  return (
    <Button size="sm" icon={dl ? RotateCw : Download} onClick={onStart}>
      {dl ? "Retry" : "Add"}
    </Button>
  );
}

/** No music to hand: search tunebat and fetch a track or two to try Seedbox 2 with. */
export function TunebatTry() {
  const [term, setTerm] = useState("");
  const [search, setSearch] = useState<Search>({ state: "idle" });
  const [downloads, setDownloads] = useState<Record<string, TunebatDownload>>({});

  useEffect(
    () =>
      onServerEvent((ev) => {
        if (ev.type !== "tunebat.download") return;
        const d = ev.data as TunebatDownload;
        setDownloads((all) => ({ ...all, [d.id]: d }));
      }),
    [],
  );

  const run = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = term.trim();
    if (!q) return;
    setSearch({ state: "searching" });
    try {
      const { items } = await api.tunebat.search(q, { quiet: true });
      setSearch({ state: "done", term: q, items });
    } catch (err) {
      setSearch({ state: "failed", error: err instanceof ApiError ? err.message : String(err) });
    }
  };
  const start = (id: string) => {
    setDownloads((all) => ({ ...all, [id]: { id, state: "queued", error: null } }));
    api.tunebat.download(id, { quiet: true }).catch((err: unknown) => {
      setDownloads((all) => ({ ...all, [id]: { id, state: "failed", error: err instanceof ApiError ? err.message : String(err) } }));
    });
  };

  return (
    <div className="flex flex-col gap-2 border-t border-line p-4">
      <form className="flex items-center gap-2" onSubmit={(e) => void run(e)}>
        <Input wrapperClassName="flex-1" autoFocus icon={Search} placeholder="A track or an artist you like" value={term} onChange={(e) => setTerm(e.target.value)} aria-label="Search tunebat" />
        <Button type="submit" disabled={!term.trim() || search.state === "searching"}>
          Search
        </Button>
      </form>
      {search.state === "searching" && (
        <div role="status" aria-label="Searching" className="flex flex-col gap-1">
          {[0, 1, 2].map((i) => (
            <div key={i} className="flex h-control-xl items-center gap-2.5">
              <Skeleton className="size-7" />
              <Skeleton className="w-1/3" />
              <span className="flex-1" />
              <Skeleton className="h-control-sm w-16" />
            </div>
          ))}
        </div>
      )}
      {search.state === "failed" && (
        <div className="flex items-center gap-2 text-xs text-danger" role="alert">
          <AlertTriangle size={14} className="shrink-0" />
          <span className="flex-1">{search.error}</span>
          <Button size="sm" icon={RotateCw} onClick={() => void run()}>
            Retry
          </Button>
        </div>
      )}
      {search.state === "done" && !search.items.length && <EmptyState compact icon={SearchX} title={`Nothing found for “${search.term}”`} description="Try an artist's name." />}
      {search.state === "done" && search.items.length > 0 && (
        <ul className="flex max-h-56 flex-col overflow-auto scroll-thin">
          {search.items.map((t) => {
            const dl = downloads[t.id];
            return (
              <li key={t.id} className="border-b border-line/60 py-1.5 last:border-b-0">
                <div className="flex items-center gap-2.5">
                  <CoverArt src={t.cover_url} size={28} camelot={t.camelot || undefined} rounded="xs" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{t.name}</div>
                    <div className="truncate text-xs text-fg-subtle">{t.artists.join(", ")}</div>
                  </div>
                  <span className="num text-xs text-fg-muted">{Math.round(t.bpm)}</span>
                  {t.camelot && <KeyBadge camelot={t.camelot} size="xs" />}
                  <span className="num w-10 text-right text-xs text-fg-subtle">{fmtTime(t.duration)}</span>
                  <DownloadAction dl={dl} onStart={() => start(t.id)} />
                </div>
                {dl?.state === "failed" && (
                  <p className="mt-1 text-xs text-danger" role="alert">
                    {dl.error}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
