/**
 * Typed client for the Seedbox 2 API (seedbox2/server/). Errors: non-2xx / network failures throw `ApiError` AND
 * show an error toast, unless `{ quiet: true }` (then the caller handles it).
 */
import { toast } from "@/store/toasts";
import type { Health, SystemInfo } from "./types";

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

export const api = {
  health: (opts?: RequestOptions) => get<Health>("/api/health", opts),
  system: (opts?: RequestOptions) => get<SystemInfo>("/api/system", opts),
};

/** Turns an audio path from the API into a playable src. */
export function resolveMediaUrl(path: string): Promise<string> {
  return Promise.resolve(apiUrl(path));
}

export function openEventStream(): EventSource {
  return new EventSource(apiUrl("/api/events"));
}
