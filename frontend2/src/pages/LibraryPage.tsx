import { FileUp, FolderPlus, Library, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { LibrarySummary, LibraryTrack } from "@/api/types";
import { Badge, Button, EmptyState, Input, Page, PageBody, PageHeader, ProgressBar, Table, TableScroll, TBody, Td, Th, THead, Tr } from "@/components/ui";
import { fmtTime, plural } from "@/lib/format";
import { importSummary, useImports } from "@/store/imports";
import { toast } from "@/store/toasts";

/** The file's own BPM tag: a hint until the analysis (#43) says otherwise. */
function tagBpm(t: LibraryTrack): string {
  const bpm = t.tags.file?.bpm;
  return typeof bpm === "number" ? bpm.toFixed(0) : "";
}

/** The latest import: a progress bar while it runs, its summary once done. */
function ImportBar() {
  const s = useImports((st) => st.status);
  if (s.state === "idle") return null;
  const running = s.state === "running" || s.state === "queued";
  const what = s.kind === "watch" ? `Scanning ${s.folder ?? "watch folders"}` : s.kind === "xml" ? `Importing playlists from ${s.label}` : `Importing ${s.label ?? "files"}`;
  return (
    <div className="flex items-center gap-3 border-b border-line bg-panel px-5 py-2 text-sm">
      {running ? (
        <>
          <span className="min-w-0 max-w-[50%] truncate text-fg-muted">{what}</span>
          <ProgressBar className="flex-1" value={s.total ? (s.done ?? 0) / s.total : null} label="Import progress" />
          <span className="shrink-0 font-mono text-xs text-fg-subtle">{s.total ? `${s.done ?? 0}/${s.total}` : "…"}</span>
        </>
      ) : (
        <span className={s.state === "failed" ? "text-danger" : "text-fg-muted"}>
          {s.state === "failed" ? `Import failed: ${s.error}` : `${s.kind === "watch" ? "Watch folders" : (s.label ?? "Import")}: ${importSummary(s)}`}
        </span>
      )}
    </div>
  );
}

export function LibraryPage() {
  const generation = useImports((s) => s.generation);
  const [summary, setSummary] = useState<LibrarySummary | null>(null);
  const [tracks, setTracks] = useState<LibraryTrack[] | null>(null);
  const [search, setSearch] = useState("");
  const [xmlPath, setXmlPath] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    void api.library.summary({ quiet: true, signal: ctrl.signal }).then(setSummary, () => {});
    void api.library.tracks({ search, limit: 500 }, { quiet: true, signal: ctrl.signal }).then((r) => setTracks(r.items), () => {});
    return () => ctrl.abort();
  }, [search, generation]);

  const addFolder = async () => {
    try {
      const { paths } = await api.library.choose("folder", { quiet: true });
      if (paths[0]) {
        await api.library.addFolder(paths[0]);
        toast.info(`Watching ${paths[0]}`, { key: "library.import", title: "Watch folders" });
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) toast.info("Add watch folders by path in Settings", { title: "No folder dialog in a browser" });
    }
  };
  const importXml = async (path?: string) => {
    try {
      const chosen = path ?? (await api.library.choose("xml", { quiet: true })).paths[0];
      if (chosen) {
        await api.library.importXml(chosen);
        setXmlPath(null);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) setXmlPath(""); // a browser: ask for the path
    }
  };

  const empty = tracks !== null && !tracks.length && !search;
  return (
    <Page>
      <PageHeader
        title="Library"
        subtitle={summary ? `${plural(summary.tracks, "track")} · ${plural(summary.crates, "crate")} · drop files or folders anywhere to add them` : "…"}
        actions={
          <>
            <Button icon={FolderPlus} onClick={() => void addFolder()}>
              Watch a folder…
            </Button>
            <Button icon={FileUp} onClick={() => void importXml()}>
              Import Rekordbox / iTunes XML…
            </Button>
          </>
        }
      >
        <div className="flex items-center gap-2 pb-3">
          <Input icon={Search} wrapperClassName="w-80" placeholder="Search title, artist, album" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search the library" />
          {xmlPath !== null && (
            <form
              className="flex flex-1 items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                void importXml(xmlPath);
              }}
            >
              <Input wrapperClassName="flex-1" autoFocus placeholder="Path to rekordbox.xml or Library.xml" value={xmlPath} onChange={(e) => setXmlPath(e.target.value)} aria-label="XML path" />
              <Button type="submit" variant="primary" disabled={!xmlPath.trim()}>
                Import
              </Button>
            </form>
          )}
        </div>
      </PageHeader>
      <ImportBar />
      <PageBody padded={false} className="flex flex-col">
        {empty ? (
          <EmptyState
            className="h-full"
            icon={Library}
            title="No music yet"
            description="Drop files or folders onto the window, watch a folder, or import your Rekordbox or iTunes/Music playlists. Every track is analysed as it comes in."
          />
        ) : (
          <TableScroll className="flex-1">
            <Table>
              <THead>
                <Tr interactive={false}>
                  <Th>Title</Th>
                  <Th>Artist</Th>
                  <Th>Album</Th>
                  <Th align="right" width={70} title="From the file's tags: a hint until the analysis">
                    BPM tag
                  </Th>
                  <Th align="right" width={70}>
                    Time
                  </Th>
                  <Th width={70}>Format</Th>
                  <Th width={100}>Source</Th>
                </Tr>
              </THead>
              <TBody>
                {tracks?.map((t) => (
                  <Tr key={t.hash}>
                    <Td truncate>{t.title}</Td>
                    <Td truncate muted>
                      {t.artist}
                    </Td>
                    <Td truncate muted>
                      {t.album}
                    </Td>
                    <Td num align="right" muted>
                      {tagBpm(t)}
                    </Td>
                    <Td num align="right" muted>
                      {fmtTime(t.duration)}
                    </Td>
                    <Td muted>{String(t.tags.file?.format ?? "").toUpperCase()}</Td>
                    <Td>
                      <Badge size="sm">{t.source}</Badge>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </TableScroll>
        )}
      </PageBody>
    </Page>
  );
}
