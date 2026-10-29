/** Files dropped in a browser: the audio files among the dropped items, folders walked (the window gets real paths from
 * pywebview instead, see seedbox2/shell.py). */

/** The formats the engine imports (seedbox2/library/tags.py AUDIO_EXTENSIONS). */
export const AUDIO_EXTENSIONS = [".mp3", ".m4a", ".aac", ".mp4", ".alac", ".flac", ".wav", ".wave", ".aif", ".aiff", ".aifc", ".ogg", ".oga", ".opus"];

export function isAudioName(name: string): boolean {
  const lower = name.toLowerCase();
  return !lower.startsWith(".") && AUDIO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/** True while dragging files (not text or a link) over the page. */
export function hasFiles(dt: DataTransfer | null): boolean {
  return !!dt && Array.from(dt.types).includes("Files");
}

/** Inside the app's window, Python receives dropped paths itself. */
export function inWindow(): boolean {
  return typeof window !== "undefined" && "pywebview" in window;
}

function readEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function walk(entry: FileSystemEntry, out: File[]): Promise<void> {
  if (entry.name.startsWith(".")) return;
  if (entry.isFile) {
    if (isAudioName(entry.name)) out.push(await fileOf(entry as FileSystemFileEntry));
    return;
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  for (;;) {
    const batch = await readEntries(reader); // a folder comes in batches of up to 100
    if (!batch.length) break;
    for (const child of batch) await walk(child, out);
  }
}

/** The audio files among the dropped items, with the files inside dropped folders. Read before awaiting anything: the
 * DataTransfer is emptied once the drop event returns. */
export async function droppedAudioFiles(dt: DataTransfer): Promise<File[]> {
  const entries = Array.from(dt.items)
    .map((item) => item.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => !!e);
  if (!entries.length) return Array.from(dt.files).filter((f) => isAudioName(f.name));
  const out: File[] = [];
  for (const e of entries) await walk(e, out);
  return out;
}
