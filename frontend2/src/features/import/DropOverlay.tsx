import { FolderDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { plural } from "@/lib/format";
import { toast } from "@/store/toasts";
import { droppedAudioFiles, hasFiles, inWindow } from "./files";

/**
 * Drop files or folders anywhere on the window to add them to the library (#40). In the app's window pywebview hands
 * Python the real paths (seedbox2/shell.py) and the import's progress comes back as `library.import` events; in a
 * browser, the audio files are uploaded into the music folder's Imported folder.
 */
export function DropOverlay() {
  const [over, setOver] = useState(false);
  const depth = useRef(0);

  useEffect(() => {
    const enter = (e: DragEvent) => {
      if (!hasFiles(e.dataTransfer)) return;
      depth.current += 1;
      setOver(true);
    };
    const overHandler = (e: DragEvent) => {
      if (!hasFiles(e.dataTransfer)) return;
      e.preventDefault(); // allow the drop
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e.dataTransfer)) return;
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setOver(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e.dataTransfer)) return;
      e.preventDefault();
      depth.current = 0;
      setOver(false);
      if (inWindow() || !e.dataTransfer) return; // Python has the paths
      void upload(droppedAudioFiles(e.dataTransfer));
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", overHandler);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", overHandler);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, []);

  if (!over) return null;
  return (
    <div className="pointer-events-none fixed inset-0 z-modal grid place-items-center bg-bg/75 p-6 backdrop-blur-sm animate-route-in">
      <div className="flex size-full flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed border-accent/70 bg-accent/5 text-center">
        <div className="grid size-14 place-items-center rounded-lg border border-accent/40 bg-raised text-accent">
          <FolderDown size={26} strokeWidth={1.75} />
        </div>
        <div className="text-lg font-semibold text-fg">Drop to add to your library</div>
        <div className="max-w-md text-sm text-fg-muted">Files or whole folders: MP3, AAC/M4A, FLAC, WAV, AIFF, OGG and Opus. Each track is analysed once it is in.</div>
      </div>
    </div>
  );
}

async function upload(pending: Promise<File[]>) {
  const files = await pending;
  if (!files.length) {
    toast.warning("No audio files in what was dropped", { key: "library.import", title: "Import" });
    return;
  }
  toast.info(`Uploading ${plural(files.length, "file")}…`, { key: "library.import", title: "Import" });
  for (const f of files) {
    try {
      await api.library.upload(f);
    } catch {
      /* toasted by the client */
    }
  }
}
