/**
 * Typed client for the Seedbox 2 API (seedbox2/server/). Errors: non-2xx / network failures throw `ApiError` AND
 * show an error toast, unless `{ quiet: true }` (then the caller handles it).
 */
import { toast } from "@/store/toasts";
import type { Health, ImportStatus, LibrarySummary, LibraryTrack, ModelInfo, ModelSettings, ModelsStatus, SystemInfo, WatchFolders } from "./types";

const BASE: string = import.meta.env.VITE_API_BASE ?? "";

export interface RequestOptions {
  /** Don't toast on failure (caller shows its own error UI). */
  quiet?: boolean;
  signal?: AbortSignal;
}

export class ApiError extends Error {
  readonly status: number; // 0 = network error / backend unreachable
  readonly detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
  get isNetwork(): boolean {
    return this.status === 0;
  }
  get isNotFound(): boolean {
    return this.status === 404;
  }
}

/** Absolute-or-relative URL for an API path ("/api/…"); use for <img>/<audio> src and EventSource. */
export function apiUrl(path: string): string {
  return /^(https?:|blob:|data:)/.test(path) ? path : `${BASE}${path}`;
}

function messageFrom(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = (detail as { detail?: unknown }).detail;
    if (typeof d === "string") return d;
  }
  return fallback;
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

export async function request<T>(method: Method, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      method,
      signal: opts.signal,
      headers: body === undefined ? { accept: "application/json" } : { accept: "application/json", "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    const err = new ApiError(0, "Can't reach the Seedbox 2 engine", e);
    if (!opts.quiet) toast.error(err.message, { key: "network", title: "Connection" });
    throw err;
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (res.ok) return data as T;
  const err = new ApiError(res.status, messageFrom(data, `${method} ${path} failed (${res.status})`), data);
  if (!opts.quiet) toast.error(err.message, { key: `${method} ${path}` });
  throw err;
}

const get = <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, undefined, opts);
const post = <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("POST", path, body, opts);

export const api = {
  health: (opts?: RequestOptions) => get<Health>("/api/health", opts),
  system: (opts?: RequestOptions) => get<SystemInfo>("/api/system", opts),
  /** Show a file or folder (inside Seedbox 2's folders) in Finder / Explorer / the file manager. */
  reveal: (path: string, opts?: RequestOptions) => post<{ ok: boolean }>("/api/system/reveal", { path }, opts),
  models: {
    status: (opts?: RequestOptions) => get<ModelsStatus>("/api/models", opts),
    /** Download one model (`force`: delete it first, i.e. re-download). Progress comes as `model.*` events. */
    download: (name: string, force = false, opts?: RequestOptions) => post<ModelInfo>(`/api/models/${encodeURIComponent(name)}/download`, { force }, opts),
    downloadMissing: (opts?: RequestOptions) => post<ModelsStatus>("/api/models/download", undefined, opts),
    cancel: (name: string, opts?: RequestOptions) => post<{ cancelled: boolean }>(`/api/models/${encodeURIComponent(name)}/cancel`, undefined, opts),
    remove: (name: string, opts?: RequestOptions) => request<ModelInfo>("DELETE", `/api/models/${encodeURIComponent(name)}`, undefined, opts),
    settings: (changes: Partial<ModelSettings>, opts?: RequestOptions) => post<ModelsStatus>("/api/models/settings", changes, opts),
  },
  library: {
    summary: (opts?: RequestOptions) => get<LibrarySummary>("/api/library", opts),
    tracks: (q: { search?: string; limit?: number; offset?: number } = {}, opts?: RequestOptions) => {
      const params = new URLSearchParams({ search: q.search ?? "", limit: String(q.limit ?? 200), offset: String(q.offset ?? 0) });
      return get<{ total: number; items: LibraryTrack[] }>(`/api/library/tracks?${params}`, opts);
    },
    folders: (opts?: RequestOptions) => get<WatchFolders>("/api/library/folders", opts),
    addFolder: (path: string, opts?: RequestOptions) => post<WatchFolders>("/api/library/folders", { path }, opts),
    removeFolder: (path: string, opts?: RequestOptions) => post<WatchFolders>("/api/library/folders/remove", { path }, opts),
    importStatus: (opts?: RequestOptions) => get<ImportStatus>("/api/library/import", opts),
    /** Import files and folders by path (the window's drop handler does this from Python). */
    importPaths: (paths: string[], opts?: RequestOptions) => post<ImportStatus>("/api/library/import", { paths }, opts),
    importXml: (path: string, opts?: RequestOptions) => post<ImportStatus>("/api/library/import/xml", { path }, opts),
    /** The OS's folder or file dialog (only in the app's window: 501 in a browser). */
    choose: (kind: "folder" | "xml", opts?: RequestOptions) => post<{ paths: string[] }>("/api/library/choose", { kind }, opts),
    /** A file dropped in a browser, which gives us its bytes but not its path. */
    upload: async (file: File, opts?: RequestOptions): Promise<ImportStatus> => {
      const res = await fetch(apiUrl(`/api/library/upload?name=${encodeURIComponent(file.name)}`), { method: "POST", body: file, signal: opts?.signal });
      const data = (await res.json().catch(() => null)) as unknown;
      if (!res.ok) {
        const err = new ApiError(res.status, messageFrom(data, `upload of ${file.name} failed (${res.status})`), data);
        if (!opts?.quiet) toast.error(err.message, { key: "upload" });
        throw err;
      }
      return data as ImportStatus;
    },
  },
};

/** Turns an audio path from the API into a playable src. */
export function resolveMediaUrl(path: string): Promise<string> {
  return Promise.resolve(apiUrl(path));
}

export function openEventStream(): EventSource {
  return new EventSource(apiUrl("/api/events"));
}
