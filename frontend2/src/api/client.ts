/**
 * Typed client for the Seedbox 2 API (seedbox2/server/). Errors: non-2xx / network failures throw `ApiError` AND
 * show an error toast, unless `{ quiet: true }` (then the caller handles it).
 */
import { toast } from "@/store/toasts";
import type { MixPlan } from "./mixplan.generated";
import type { PowerMode, PowerStatus, CrateSummary, Health, ImportStatus, JobsResponse, LibraryRow, LibrarySummary, LibraryTrack, KeyStatus, ListenImport, ListenRating, ListenStatus, ListenSummary, ListenTrial, ModelInfo, ModelSettings, ModelsStatus, Onboarding, SetStemsStatus, SetSummary, StemsStatus, SystemInfo, TunebatDownload, TunebatTrack, WatchFolders } from "./types";

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
const put = <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("PUT", path, body, opts);

export const api = {
  health: (opts?: RequestOptions) => get<Health>("/api/health", opts),
  system: (opts?: RequestOptions) => get<SystemInfo>("/api/system", opts),
  /** Show a file or folder (inside Seedbox 2's folders) in Finder / Explorer / the file manager. */
  reveal: (path: string, opts?: RequestOptions) => post<{ ok: boolean }>("/api/system/reveal", { path }, opts),
  listen: {
    status: (rater: string, opts?: RequestOptions) => get<ListenStatus>(`/api/listen/status?rater=${encodeURIComponent(rater)}`, opts),
    /** A new blind trial: only the pair's name and an id say anything about it. */
    next: (rater: string, opts?: RequestOptions) => get<ListenTrial>(`/api/listen/next?rater=${encodeURIComponent(rater)}`, opts),
    audioUrl: (trialId: string, slot: "a" | "b") => `/api/listen/trials/${encodeURIComponent(trialId)}/${slot}`,
    rate: (trialId: string, rating: ListenRating, opts?: RequestOptions) => post<{ ok: boolean; ratings: number }>(`/api/listen/trials/${encodeURIComponent(trialId)}/rating`, rating, opts),
    summary: (opts?: RequestOptions) => get<ListenSummary>("/api/listen/summary", opts),
    /** Writes a ratings file in the listening folder: a browser download doesn't work in every app window. */
    saveExport: (rater: string, opts?: RequestOptions) => post<{ path: string; ratings: number }>("/api/listen/export/save", { rater }, opts),
    import: (doc: unknown, opts?: RequestOptions) => post<ListenImport>("/api/listen/import", doc, opts),
  },
  mixes: {
    plans: (opts?: RequestOptions) => get<SavedPlan[]>("/api/mixes/plans", { quiet: true, ...opts }),
    /** A saved plan as v3, for the live engine. */
    livePlan: (from: string, to: string, opts?: RequestOptions) => get<{ plan: MixPlan; user_edited: boolean }>(`/api/mixes/plans/${from}/${to}/live`, { quiet: true, ...opts }),
  },
  onboarding: {
    status: (opts?: RequestOptions) => get<Onboarding>("/api/onboarding", opts),
    finish: (opts?: RequestOptions) => post<Onboarding>("/api/onboarding", { done: true }, opts),
  },
  tunebat: {
    search: (q: string, opts?: RequestOptions) => get<{ total: number; items: TunebatTrack[] }>(`/api/tunebat/search?q=${encodeURIComponent(q)}`, opts),
    /** Fetch a search result into the music folder. Progress comes as `tunebat.download` events. */
    download: (id: string, opts?: RequestOptions) => post<TunebatDownload>("/api/tunebat/download", { id }, opts),
  },
  models: {
    status: (opts?: RequestOptions) => get<ModelsStatus>("/api/models", opts),
    /** Download one model (`force`: delete it first, i.e. re-download). Progress comes as `model.*` events. */
    download: (name: string, force = false, opts?: RequestOptions) => post<ModelInfo>(`/api/models/${encodeURIComponent(name)}/download`, { force }, opts),
    downloadMissing: (opts?: RequestOptions) => post<ModelsStatus>("/api/models/download", undefined, opts),
    cancel: (name: string, opts?: RequestOptions) => post<{ cancelled: boolean }>(`/api/models/${encodeURIComponent(name)}/cancel`, undefined, opts),
    remove: (name: string, opts?: RequestOptions) => request<ModelInfo>("DELETE", `/api/models/${encodeURIComponent(name)}`, undefined, opts),
    settings: (changes: Partial<ModelSettings>, opts?: RequestOptions) => post<ModelsStatus>("/api/models/settings", changes, opts),
  },
  power: {
    status: (opts?: RequestOptions) => get<PowerStatus>("/api/power", opts),
    setMode: (mode: PowerMode, opts?: RequestOptions) => post<PowerStatus>("/api/power/settings", { mode }, opts),
  },
  key: {
    /** Before it is computed (202) the answer is the queued job: `job.done` says when to ask again. */
    status: (hash: string, opts?: RequestOptions) => get<KeyStatus | { state: string }>(`/api/analysis/${hash}/key`, opts),
    override: (hash: string, key: string, opts?: RequestOptions) => request<KeyStatus>("PUT", `/api/analysis/${hash}/key/override`, { key }, opts),
    clearOverride: (hash: string, opts?: RequestOptions) => request<KeyStatus>("DELETE", `/api/analysis/${hash}/key/override`, undefined, opts),
  },
  stems: {
    status: (opts?: RequestOptions) => get<StemsStatus>("/api/analysis/stems", opts),
    settings: (changes: Partial<Omit<StemsStatus, "usage">>, opts?: RequestOptions) => post<StemsStatus>("/api/analysis/stems/settings", changes, opts),
    /** The "stems ready for this set" indicator; `stems.set` events say when to ask again. */
    forSet: (setId: number, opts?: RequestOptions) => get<SetStemsStatus>(`/api/sets/${setId}/stems`, opts),
    /** The set is open: its missing stems go first. */
    openSet: (setId: number, opts?: RequestOptions) => post<SetStemsStatus>(`/api/sets/${setId}/stems`, undefined, opts),
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
    /** Every track as a table row (#73); `hashes`: only these. */
    view: (hashes?: string[], opts?: RequestOptions) =>
      hashes ? post<{ total: number; items: LibraryRow[] }>("/api/library/view", { hashes }, opts) : get<{ total: number; items: LibraryRow[] }>("/api/library/view", opts),
    /** The overview waveforms of these tracks, binary (features/library/overviews.ts parses it). */
    overviews: async (hashes: string[], opts?: RequestOptions): Promise<ArrayBuffer> => {
      const res = await fetch(apiUrl("/api/library/overviews"), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ hashes }),
        signal: opts?.signal,
      });
      if (!res.ok) throw new ApiError(res.status, `overviews failed (${res.status})`);
      return res.arrayBuffer();
    },
    crates: (opts?: RequestOptions) => get<CrateSummary[]>("/api/library/crates", opts),
    newCrate: (name: string, hashes: string[], opts?: RequestOptions) => post<CrateSummary>("/api/library/crates", { name, hashes }, opts),
    addToCrate: (id: number, hashes: string[], opts?: RequestOptions) => post<{ added: number; hashes: string[] }>(`/api/library/crates/${id}/tracks`, { hashes }, opts),
    crateHashes: (id: number, opts?: RequestOptions) => get<{ hashes: string[] }>(`/api/library/crates/${id}/hashes`, opts),
    smartCrates: (opts?: RequestOptions) => get<{ crates: SmartCrateJson[] }>("/api/library/smart-crates", opts),
    saveSmartCrates: (crates: SmartCrateJson[], opts?: RequestOptions) => put<{ crates: SmartCrateJson[] }>("/api/library/smart-crates", { crates }, opts),
  },
  sets: {
    list: (opts?: RequestOptions) => get<SetSummary[]>("/api/sets", opts),
    create: (name: string, hashes: string[], opts?: RequestOptions) => post<SetSummary>("/api/sets", { name, hashes }, opts),
    addTracks: (id: number, hashes: string[], opts?: RequestOptions) => post<{ added: number; hashes: string[] }>(`/api/sets/${id}/tracks`, { hashes }, opts),
    hashes: (id: number, opts?: RequestOptions) => get<{ hashes: string[] }>(`/api/sets/${id}/hashes`, opts),
  },
  jobs: {
    list: (state?: string, opts?: RequestOptions) => get<JobsResponse>(`/api/jobs${state ? `?state=${state}` : ""}`, opts),
    submit: (kind: string, payload: Record<string, unknown>, opts?: RequestOptions) => post<unknown>("/api/jobs", { kind, payload }, opts),
  },
};

/** A saved filter (smart crate), as settings.json keeps it. */
/** A pair's saved mix plan, without the plan. */
export interface SavedPlan {
  from_hash: string;
  to_hash: string;
  from_name: string;
  to_name: string;
  user_edited: boolean;
}

export interface SmartCrateJson {
  id: string;
  name: string;
  filter: Record<string, unknown>;
}

/** Turns an audio path from the API into a playable src. */
export function resolveMediaUrl(path: string): Promise<string> {
  return Promise.resolve(apiUrl(path));
}

export function openEventStream(): EventSource {
  return new EventSource(apiUrl("/api/events"));
}
