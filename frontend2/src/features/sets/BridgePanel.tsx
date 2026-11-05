/**
 * What could go between two neighbours that don't mix well (#64): the library first, then, on request, tunebat's
 * recommendations of both, downloaded through the same path as the first run's.
 */
import { Globe, Plus } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { LibraryBridge, LibraryBridges, OnlineBridges, ServerEvent, SetOrderResult, TunebatDownload } from "@/api/types";
import { Button, KeyBadge, Skeleton } from "@/components/ui";
import { DownloadAction } from "@/features/onboarding/TunebatTry";

export interface BridgePanelProps {
  setId: number;
  /** The transition's first track. */
  position: number;
  onInserted: (result: SetOrderResult) => void;
}

/** A download's new track can only be a candidate once these have run. */
const ANALYSED_BY = new Set(["beats", "key"]);

function Scores({ a, b }: { a: number; b: number }) {
  return (
    <span className="num w-14 shrink-0 text-right text-xs text-fg-muted" title="How well it would mix with each neighbour, out of 100">
      {a} · {b}
    </span>
  );
}

function LibraryRow({ track, adding, onAdd }: { track: LibraryBridge; adding: boolean; onAdd: () => void }) {
  return (
    <li className="flex items-center gap-3 py-1">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm">
          <span className="text-fg">{track.title}</span>
          {track.artist && <span className="text-fg-subtle"> · {track.artist}</span>}
        </div>
        <div className="truncate text-xs text-fg-subtle">{track.text}</div>
      </div>
      <KeyBadge camelot={track.key} size="xs" className="shrink-0" />
      <Scores a={track.score_in} b={track.score_out} />
      <Button size="sm" icon={Plus} loading={adding} onClick={onAdd}>
        Add here
      </Button>
    </li>
  );
}

export function BridgePanel({ setId, position, onInserted }: BridgePanelProps) {
  const [library, setLibrary] = useState<LibraryBridges | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [online, setOnline] = useState<OnlineBridges | null>(null);
  const [onlineState, setOnlineState] = useState<"idle" | "searching" | "failed">("idle");
  const [onlineError, setOnlineError] = useState<string | null>(null);
  const [downloads, setDownloads] = useState<Record<string, TunebatDownload>>({});
  const [adding, setAdding] = useState<string | null>(null);
  const fetched = useRef(false); // a download finished: its track is worth looking for as jobs finish

  const search = useCallback(
    (signal?: AbortSignal) =>
      api.sets.bridge(setId, position, { quiet: true, signal }).then(
        (r) => {
          setLibrary(r);
          setError(null);
        },
        (e: unknown) => {
          if (!signal?.aborted) setError((e as Error).message);
        },
      ),
    [setId, position],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void search(ctrl.signal);
    return () => ctrl.abort();
  }, [search]);

  useEffect(
    () =>
      onServerEvent((ev: ServerEvent) => {
        if (ev.type === "tunebat.download") {
          const d = ev.data as TunebatDownload;
          setDownloads((all) => ({ ...all, [d.id]: d }));
          if (d.state === "done") fetched.current = true;
        } else if (ev.type === "job.done" && fetched.current && ANALYSED_BY.has((ev.data as { kind?: string }).kind ?? "")) {
          void search();
        }
      }),
    [search],
  );

  const add = async (hash: string) => {
    if (!library) return;
    setAdding(hash);
    try {
      onInserted(await api.sets.insertBridge(setId, { position, hash, from: library.gap.from, to: library.gap.to }));
    } catch {
      setAdding(null); // the client has shown it
    }
  };

  const lookOnline = async () => {
    setOnlineState("searching");
    try {
      setOnline(await api.sets.bridgeOnline(setId, position, { quiet: true }));
      setOnlineState("idle");
    } catch (e) {
      setOnlineError((e as Error).message);
      setOnlineState("failed");
    }
  };

  const download = (id: string) => {
    setDownloads((all) => ({ ...all, [id]: { id, state: "queued", error: null } }));
    api.tunebat.download(id, { quiet: true }).catch((e: unknown) => {
      setDownloads((all) => ({ ...all, [id]: { id, state: "failed", error: (e as Error).message } }));
    });
  };

  if (error) return <p className="px-4 py-3 pl-20 text-xs text-danger">{error}</p>;
  if (!library) return <Skeleton className="mx-4 my-3 h-16" />;

  const unknown = online?.seeds.filter((s) => s.id === null).length ?? 0;
  return (
    <div className="flex flex-col gap-2 bg-surface py-3 pl-20 pr-3">
      <p className="text-xs text-fg-muted">Look for {library.want.text}.</p>
      {library.results.length ? (
        <ul className="flex flex-col">
          {library.results.map((t) => (
            <LibraryRow key={t.hash} track={t} adding={adding === t.hash} onAdd={() => void add(t.hash)} />
          ))}
        </ul>
      ) : (
        <p className="text-xs text-fg-subtle">Nothing in the library does better than this transition.</p>
      )}
      {library.unanalysed > 0 && <p className="text-xs text-fg-subtle">{library.unanalysed} more tracks wait for their tempo or key.</p>}

      {online ? (
        <>
          <p className="pt-1 text-xs text-fg-muted">From tunebat: its tempo and key, not analysed yet.</p>
          {online.results.length ? (
            <ul className="flex flex-col">
              {online.results.map((t) => {
                const dl = downloads[t.id];
                return (
                  <li key={t.id} className="flex items-center gap-3 py-1">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm">
                        <span className="text-fg">{t.name}</span>
                        <span className="text-fg-subtle"> · {t.artists.join(", ")}</span>
                      </div>
                      <div className="truncate text-xs italic text-fg-subtle">{dl?.state === "failed" && dl.error ? dl.error : t.text}</div>
                    </div>
                    <KeyBadge camelot={t.camelot} size="xs" className="shrink-0" />
                    <Scores a={t.score_in} b={t.score_out} />
                    <DownloadAction dl={dl} onStart={() => download(t.id)} />
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-xs text-fg-subtle">Nothing it recommends does better than this transition.</p>
          )}
          {unknown > 0 && <p className="text-xs text-fg-subtle">Tunebat doesn't know {unknown === 1 ? "one of the two tracks" : "either track"}.</p>}
          {Object.values(downloads).some((d) => d.state === "done") && (
            <p className="text-xs text-fg-subtle">A downloaded track shows up in the list above once it's analysed.</p>
          )}
        </>
      ) : (
        <div className="flex items-center gap-3 pt-1">
          <Button size="sm" icon={Globe} loading={onlineState === "searching"} onClick={() => void lookOnline()}>
            Look on tunebat
          </Button>
          {onlineState === "failed" && <span className="text-xs text-danger">{onlineError}</span>}
        </div>
      )}
    </div>
  );
}
