import { FolderOpen, FolderPlus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { WatchFolders } from "@/api/types";
import { Button, IconButton, Input, InfoRow, Panel, PanelHeader, Spinner, StatusDot } from "@/components/ui";
import { tildify } from "@/lib/format";
import { useApp } from "@/store/app";
import { useImports } from "@/store/imports";

/** Settings: the folders the engine scans and then watches for new, changed and removed music (#40). */
export function WatchFoldersPanel() {
  const system = useApp((s) => s.system);
  const generation = useImports((s) => s.generation);
  const scanning = useImports((s) => (s.status.state === "running" && s.status.kind === "watch" ? s.status : null));
  const [data, setData] = useState<WatchFolders | null>(null);
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => void api.library.folders({ quiet: true }).then(setData, () => {}), []);
  useEffect(reload, [reload, generation]);

  const add = async (p: string) => {
    if (!p.trim()) return;
    try {
      setData(await api.library.addFolder(p.trim(), { quiet: true }));
      setPath("");
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };
  const choose = async () => {
    try {
      const { paths } = await api.library.choose("folder", { quiet: true });
      if (paths[0]) await add(paths[0]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) setError("Type the folder's path: there is no folder dialog in a browser");
    }
  };

  return (
    <Panel className="max-w-3xl">
      <PanelHeader
        title="Watch folders"
        description="Scanned now, then followed: new, changed, moved and deleted files keep the library in step"
        actions={
          <Button size="sm" icon={FolderPlus} onClick={() => void choose()}>
            Add folder…
          </Button>
        }
      />
      {data?.folders.map((f) => (
        <InfoRow key={f.path} label={f.exists ? (scanning?.folder === f.path ? "Scanning" : "Watching") : "Missing"}>
          <StatusDot tone={f.exists ? "ok" : "warn"} />
          <span className="min-w-0 flex-1 break-all font-mono text-xs">{tildify(f.path)}</span>
          {scanning?.folder === f.path && (
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-fg-subtle">
              <Spinner size={12} /> {scanning.done ?? 0}/{scanning.total ?? "…"}
            </span>
          )}
          {f.exists && system && <IconButton icon={FolderOpen} label={system.reveal_label} tooltip={system.reveal_label} size="sm" onClick={() => void api.reveal(f.path, { quiet: true })} />}
          <IconButton icon={Trash2} label="Stop watching" tooltip="Stop watching (the tracks stay)" size="sm" onClick={() => void api.library.removeFolder(f.path).then(setData)} />
        </InfoRow>
      ))}
      {data && !data.folders.length && <InfoRow label="None">Nothing is watched: add a folder of music</InfoRow>}
      <form
        className="flex items-center gap-2 px-4 py-2.5"
        onSubmit={(e) => {
          e.preventDefault();
          void add(path);
        }}
      >
        <Input wrapperClassName="flex-1" placeholder="/path/to/music" value={path} invalid={!!error} onChange={(e) => setPath(e.target.value)} aria-label="Folder to watch" />
        <Button type="submit" disabled={!path.trim()}>
          Watch
        </Button>
      </form>
      {error && <p className="px-4 pb-2.5 text-xs text-danger">{error}</p>}
    </Panel>
  );
}
