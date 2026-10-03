/**
 * Typed API client for every endpoint in APP_SPEC.md. All functions return the types.ts shapes.
 * Errors: non-2xx / network failures throw `ApiError` AND show an error toast, unless `{ quiet: true }`
 * (then the caller handles it). With VITE_MOCK=1 the same calls are served by src/mock/server.ts.
 */
import { toast } from "@/store/toasts";
import type {
  OkResponse,
  PickFolderResult,
  UpdateResult,
  Crate,
  CrateSet,
  CrateSummary,
  Job,
  Partner,
  PreviewStatus,
  SearchPage,
  Settings,
  SystemStatus,
  TrackAnalysis,
} from "./types";

export const MOCK = import.meta.env.VITE_MOCK === "1";
const BASE: string = import.meta.env.VITE_API_BASE ?? "";

/** POST /api/system/update-ytdlp result. The new version applies on next launch. */
export type YtdlpUpdateResult = UpdateResult;
export type OkResult = OkResponse;

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
  /** 409: the crate has a running download job (order changes are refused until it finishes). */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

/** Absolute-or-relative URL for an API path ("/api/…"); use for <img>/<audio> src and EventSource. */
export function apiUrl(path: string): string {
  return /^(https?:|blob:|data:)/.test(path) ? path : `${BASE}${path}`;
}

const enc = encodeURIComponent;

function qs(params: Record<string, string | number | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") p.set(k, String(v));
  return p.toString();
}

function messageFrom(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = (detail as { detail?: unknown; message?: unknown }).detail ?? (detail as { message?: unknown }).message;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? String(x.msg) : String(x))).join("; ");
  }
  return fallback;
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

async function send(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<{ status: number; data: unknown }> {
  if (MOCK) {
    const { mockFetch } = await import("@/mock/server");
    return mockFetch(method, path, body);
  }
  const res = await fetch(apiUrl(path), {
    method,
    signal,
    headers: body === undefined ? { accept: "application/json" } : { accept: "application/json", "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  return { status: res.status, data };
}

export async function request<T>(method: Method, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  let status: number;
  let data: unknown;
  try {
    ({ status, data } = await send(method, path, body, opts.signal));
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    const err = new ApiError(0, "Can't reach the Seedbox engine", e);
    if (!opts.quiet) toast.error(err.message, { key: "network", title: "Connection" });
    throw err;
  }
  if (status >= 200 && status < 300) return data as T;
  const err = new ApiError(status, messageFrom(data, `${method} ${path} failed (${status})`), data);
  if (!opts.quiet) toast.error(err.message, { key: `${method} ${path}` });
  throw err;
}

const get = <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, undefined, opts);
const post = <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("POST", path, body ?? {}, opts);
const put = <T>(path: string, body: unknown, opts?: RequestOptions) => request<T>("PUT", path, body, opts);
const del = <T>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, undefined, opts);

export const api = {
  health: (opts?: RequestOptions) => get<OkResult>("/api/health", { quiet: true, ...opts }),

  // system + settings
  system: (opts?: RequestOptions) => get<SystemStatus>("/api/system", opts),
  updateYtdlp: (opts?: RequestOptions) => post<YtdlpUpdateResult>("/api/system/update-ytdlp", undefined, opts),
  /** Native folder dialog (blocks until the user answers): {path} or {path: null} if cancelled. 501 outside the app window, 409 if one is already open. */
  pickFolder: (initial?: string, opts?: RequestOptions) => post<PickFolderResult>("/api/system/pick-folder", initial ? { initial } : {}, opts),
  settings: (opts?: RequestOptions) => get<Settings>("/api/settings", opts),
  saveSettings: (patch: Partial<Settings>, opts?: RequestOptions) => put<Settings>("/api/settings", patch, opts),

  // search → crate
  /** term may be free text, a Spotify/tunebat URL or a 22-char track ID. */
  search: (term: string, page = 1, opts?: RequestOptions) => get<SearchPage>(`/api/search?${qs({ term, page })}`, opts),
  /** Builds the crate for a seed and starts preview preloading (watch `preview` events). */
  newCrate: (seedId: string, opts?: RequestOptions) => get<Crate>(`/api/crates/new/${enc(seedId)}`, opts),

  // previews
  preview: (trackId: string, opts?: RequestOptions) => get<PreviewStatus>(`/api/previews/${enc(trackId)}`, opts),
  prioritizePreview: (trackId: string, opts?: RequestOptions) =>
    post<PreviewStatus>(`/api/previews/${enc(trackId)}/prioritize`, undefined, opts),
  previewAudioPath: (trackId: string) => `/api/previews/${enc(trackId)}/audio`,

  // download jobs
  startJob: (seedId: string, trackIds: string[], opts?: RequestOptions) =>
    post<Job>("/api/jobs", { seed_id: seedId, track_ids: trackIds }, opts),
  /** In-memory job history of this engine session (restores Downloads after a reload). */
  jobs: (opts?: RequestOptions) => get<Job[]>("/api/jobs", opts),
  job: (jobId: string, opts?: RequestOptions) => get<Job>(`/api/jobs/${enc(jobId)}`, opts),
  /** Cancel: queued items end as status "failed", reason "cancelled". */
  cancelJob: (jobId: string, opts?: RequestOptions) => del<Job>(`/api/jobs/${enc(jobId)}`, opts),

  // crate library + set builder
  crates: (opts?: RequestOptions) => get<CrateSummary[]>("/api/crates", opts),
  crateSet: (folder: string, opts?: RequestOptions) => get<CrateSet>(`/api/crates/${enc(folder)}/set`, opts),
  saveOrder: (folder: string, trackIds: string[], opts?: RequestOptions) =>
    put<CrateSet>(`/api/crates/${enc(folder)}/order`, { track_ids: trackIds }, opts),
  autoOrder: (folder: string, opts?: RequestOptions) => post<CrateSet>(`/api/crates/${enc(folder)}/auto-order`, undefined, opts),
  partners: (folder: string, trackId: string, direction: "out" | "in" = "out", opts?: RequestOptions) =>
    get<Partner[]>(`/api/crates/${enc(folder)}/partners/${enc(trackId)}?${qs({ direction })}`, opts),
  /**
   * TrackAnalysis, or null while it is queued/running (404). Never toasts: a failed analysis throws ApiError 422
   * ("analysis failed: …") for the caller to show inline (503 = analysis module unavailable).
   */
  analysis: async (folder: string, trackId: string, opts?: RequestOptions): Promise<TrackAnalysis | null> => {
    try {
      return await get<TrackAnalysis>(`/api/analysis/${enc(folder)}/${enc(trackId)}`, { ...opts, quiet: true });
    } catch (e) {
      if (e instanceof ApiError && e.isNotFound) return null;
      throw e;
    }
  },
  filePath: (folder: string, filename: string) => `/api/files/${enc(folder)}/${enc(filename)}`,

  /** Reveal a file/folder in Finder (must be under the output root). */
  reveal: (path: string, opts?: RequestOptions) => post<OkResult>("/api/reveal", { path }, opts),
};

/** Turns an audio path from the API ("/api/previews/<id>/audio", SetTrack.audio_url) into a playable src. */
export async function resolveMediaUrl(path: string): Promise<string> {
  if (MOCK) {
    const { mockAudioUrl } = await import("@/mock/audio");
    return mockAudioUrl(path);
  }
  return apiUrl(path);
}

/** Minimal EventSource surface (real EventSource or the mock stream). */
export interface EventStream {
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent<string>) => void) | null;
  onerror: ((ev: Event) => void) | null;
  readonly readyState: number;
  close(): void;
}

export async function openEventStream(): Promise<EventStream> {
  if (MOCK) {
    const { MockEventSource } = await import("@/mock/server");
    return new MockEventSource();
  }
  return new EventSource(apiUrl("/api/events")) as EventStream;
}
