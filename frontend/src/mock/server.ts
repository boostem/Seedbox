/**
 * In-browser fake of the Seedbox backend for VITE_MOCK=1: every endpoint in APP_SPEC.md + an SSE stream that
 * animates preview preloading, download jobs and analysis progress. State lives in memory (resets on reload).
 */
import type { EventStream } from "@/api/client";
import type { Crate, CrateLink, CrateSet, Job, JobItem, MixPlan, Partner, PreviewStatus, ServerEvent, Setlist, SetMix, Settings, SetTrack, SystemStatus, Track } from "@/api/types";
import { mockAnalysis, mockTransition } from "./analysis";
import { emitRenderProgress, generateMixPlan, mockRender, mockStyle, validateMixPlan } from "./mixplan";
import { crateSummary, energyRatings, FAVOUR, genreFor, HOME, initialCrates, type MockCrate, orderSet, POOL, recsFor, sanitize, SEARCH_PAGE } from "./data";
import { between, hash, rng, sleep } from "./rng";

type AnalysisState = SetTrack["analysis_state"];
type Method = "GET" | "POST" | "PUT" | "DELETE";
interface Resp {
  status: number;
  data: unknown;
}

const clone = <T>(x: T): T => structuredClone(x);
const enc = encodeURIComponent;
const ok = (data: unknown): Resp => ({ status: 200, data: clone(data) });
const fail = (status: number, detail: string): Resp => ({ status, data: { detail } });

// ---------------------------------------------------------------------------------------------------- state

const settings: Settings = {
  output_root: `${HOME}/Music/MusicReco`,
  workers: 2,
  tolerance_s: 15,
  bpm_tolerance: 0.1,
  preview_concurrency: 3,
  preview_cache_mb: 500,
  pretick_all: true,
};

const system: SystemStatus = {
  version: "0.3.0",
  ytdlp_version: "2026.09.12",
  ytdlp_latest: "2026.09.28",
  ffmpeg: "/Applications/Seedbox.app/Contents/Frameworks/bin/ffmpeg",
  deno: "/Applications/Seedbox.app/Contents/Frameworks/bin/deno",
  output_root: settings.output_root,
};

const crates: MockCrate[] = initialCrates();
const analysis = new Map<string, Map<string, AnalysisState>>();
const analysing = new Set<string>();
const previews = new Map<string, PreviewStatus>();
let previewQueue: string[] = [];
let previewActive = 0;
const jobs = new Map<string, Job>();
let jobSeq = 0;

// pre-seed analysis states: mostly ready, a few pending/error so the UI shows every state
crates.forEach((c, ci) => {
  const m = new Map<string, AnalysisState>();
  c.trackIds.forEach((id, i) => {
    let s: AnalysisState = "ready";
    if (ci === 0 && i === 5) s = "running";
    if (ci === 0 && (i === 3 || i === 7)) s = "queued";
    if (ci === crates.length - 1 && i === 2) s = "error";
    if (ci === crates.length - 1 && i === 4) s = "missing";
    m.set(id, s);
  });
  analysis.set(c.folder, m);
});

const crateByFolder = (folder: string) => crates.find((c) => c.folder === folder);
const track = (id: string) => POOL.get(id);
const crateDir = (folder: string) => `${settings.output_root}/${folder}`;
const fileName = (t: Track) => `${sanitize(t.display_name)}.mp3`;

// ---------------------------------------------------------------------------------------------------- SSE

const sources = new Set<MockEventSource>();

function emit(ev: ServerEvent): void {
  const data = JSON.stringify(ev);
  for (const s of sources) s.deliver(data);
}

/** Drop-in for EventSource('/api/events'). */
export class MockEventSource implements EventStream {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent<string>) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  constructor() {
    sources.add(this);
    setTimeout(() => {
      if (this.readyState === 2) return;
      this.readyState = 1;
      this.onopen?.(new Event("open"));
    }, 30);
  }
  deliver(data: string) {
    if (this.readyState === 1) this.onmessage?.(new MessageEvent("message", { data }));
  }
  close() {
    this.readyState = 2;
    sources.delete(this);
  }
}

// ---------------------------------------------------------------------------------------------------- previews

function previewStatus(id: string): PreviewStatus {
  return previews.get(id) ?? { track_id: id, state: "idle", audio_url: null, peaks: null, youtube_title: null, error: null };
}

function setPreview(p: PreviewStatus) {
  previews.set(p.track_id, p);
  emit({ type: "preview", data: clone(p) });
}

function clipPeaks(id: string): number[] {
  const R = rng(`pk:${id}`);
  const t = track(id);
  const beatsPerClip = ((t?.bpm || 124) / 60) * 20;
  const out: number[] = [];
  const breakAt = R() < 0.4 ? Math.floor(between(R, 90, 150)) : -1;
  for (let i = 0; i < 200; i++) {
    const beatPh = ((i / 200) * beatsPerClip) % 1;
    const kick = Math.exp(-beatPh * 7) * 0.35;
    const inBreak = breakAt > 0 && i >= breakAt && i < breakAt + 24;
    const base = inBreak ? 0.22 : 0.48 + 0.12 * Math.sin(i / 17 + R());
    out.push(Math.round(Math.min(1, Math.max(0.04, base + (inBreak ? 0 : kick) + (R() - 0.5) * 0.14)) * 1000) / 1000);
  }
  return out;
}

function pumpPreviews() {
  while (previewActive < settings.preview_concurrency && previewQueue.length) startPreview(previewQueue.shift()!);
}

function startPreview(id: string) {
  const cur = previewStatus(id);
    if (cur.state === "ready" || cur.state === "no_match" || cur.state === "loading") return;
    previewActive++;
    setPreview({ ...cur, state: "loading", error: null });
    const R = rng(`pv:${id}:${Date.now() % 7}`);
    setTimeout(() => {
      previewActive--;
      const t = track(id);
      const h = hash(id);
      if (h % 9 === 4) {
        setPreview({ ...previewStatus(id), state: "no_match", error: `no result within ±${settings.tolerance_s}s (closest ${Math.floor(((t?.duration_s ?? 200) + 41) / 60)}:${String(Math.round(((t?.duration_s ?? 200) + 41) % 60)).padStart(2, "0")} on ytmusic)` });
      } else if (h % 37 === 11) {
        setPreview({ ...previewStatus(id), state: "error", youtube_title: `${t?.display_name ?? id} (Official Audio)`, error: "HTTP Error 403: Forbidden" });
      } else {
        setPreview({
          track_id: id,
          state: "ready",
          audio_url: `/api/previews/${enc(id)}/audio`,
          peaks: clipPeaks(id),
          youtube_title: h % 3 ? `${t?.artists[0] ?? ""} - ${t?.name ?? id}` : `${t?.display_name ?? id} (Official Audio)`,
          error: null,
        });
      }
      pumpPreviews();
    }, between(R, 500, 2400));
}

function preload(ids: string[]) {
  previewQueue = []; // opening a crate drops queued (not in-flight) work
  for (const id of ids) {
    const s = previewStatus(id).state;
    if (s === "ready" || s === "no_match" || s === "loading" || previewQueue.includes(id)) continue;
    previewQueue.push(id);
    setPreview({ ...previewStatus(id), state: "queued" });
  }
  pumpPreviews();
}

// ---------------------------------------------------------------------------------------------------- crate set + analysis

function crateSet(c: MockCrate): CrateSet {
  const states = analysis.get(c.folder) ?? new Map<string, AnalysisState>();
  const tracks = c.trackIds.map((id) => track(id)).filter((t): t is Track => !!t);
  const ratings = energyRatings(tracks);
  const setTracks: SetTrack[] = tracks.map((t, i) => ({
    track: t,
    path: `${crateDir(c.folder)}/${fileName(t)}`,
    audio_url: `/api/files/${enc(c.folder)}/${enc(fileName(t))}`,
    order: i + 1,
    energy_rating: ratings[t.id] ?? null,
    genre: genreFor(t),
    analysis_state: states.get(t.id) ?? "missing",
  }));
  const an = (t: Track) => (states.get(t.id) === "ready" ? mockAnalysis(t) : null);
  const transitions = tracks.slice(1).map((b, i) => {
    const t = mockTransition(tracks[i], an(tracks[i]), b, an(b), settings.bpm_tolerance);
    return { ...t, style: t.ready ? mockStyle(t.technique ?? "phrase blend", tracks[i].id + b.id) : null };
  });
  return { folder: c.folder, path: crateDir(c.folder), seed_id: c.seed?.id ?? null, tracks: setTracks, transitions };
}

/** Moves missing/queued/running analyses to ready (or error) over a few seconds, with events. */
async function runAnalyses(folder: string) {
  if (analysing.has(folder)) return;
  const states = analysis.get(folder);
  if (!states) return;
  const pending = [...states].filter(([, s]) => s === "missing" || s === "queued" || s === "running").map(([id]) => id);
  if (!pending.length) return;
  analysing.add(folder);
  for (const id of pending) {
    if (states.get(id) === "missing") {
      states.set(id, "queued");
      emit({ type: "analysis", data: { folder, track_id: id, state: "queued" } });
    }
  }
  for (const id of pending) {
    await sleep(900);
    states.set(id, "running");
    emit({ type: "analysis", data: { folder, track_id: id, state: "running" } });
    await sleep(1300);
    states.set(id, "ready");
    emit({ type: "analysis", data: { folder, track_id: id, state: "ready" } });
  }
  analysing.delete(folder);
  emit({ type: "set_changed", data: { folder } });
}

function touch(c: MockCrate) {
  c.updatedAt = new Date().toISOString();
}

// ---------------------------------------------------------------------------------------------------- jobs

const STEPS = ["searching", "downloading 8%", "downloading 41%", "downloading 77%", "downloading 100%", "genre lookup", "loudness", "tagging"];

async function processItem(job: Job, it: JobItem, existing: Set<string>, otherCrates: Set<string>) {
  const id = it.track.id;
  const say = (text: string) => {
    it.text = text;
    emit({ type: "job", data: { job_id: job.job_id, track_id: id, text } });
  };
  const R = rng(`job:${id}`);
  const finish = (status: JobItem["status"], reason: string | null = null) => {
    it.status = status;
    it.reason = reason;
    if (status === "downloaded" || status === "copied" || status === "exists") {
      it.path = `${crateDir(job.folder)}/${fileName(it.track)}`;
      it.genre = genreFor(it.track);
    }
    say(status ?? "failed");
  };
  if (existing.has(id)) {
    await sleep(150);
    return finish("exists");
  }
  if (otherCrates.has(id)) {
    say("copying");
    await sleep(500);
    return finish("copied");
  }
  for (const step of STEPS) {
    if (job.state === "cancelled") return;
    say(step);
    await sleep(between(R, 220, 650));
    if (step === "searching" && hash(id) % 11 === 7) return finish("skipped", `no YouTube match within ±${settings.tolerance_s}s (closest ${Math.floor(it.track.duration_s / 60) + 1}:02)`);
    if (step === "downloading 41%" && hash(id) % 29 === 13) return finish("failed", "HTTP Error 403: Forbidden (after 3 attempts)");
  }
  it.youtube_url = `https://music.youtube.com/watch?v=${id.slice(0, 11)}`;
  finish("downloaded");
}

const folderJobs = new Map<string, Promise<void>>();

/** Jobs for the same crate run one after another (a second one waits with every item "queued"). */
function runJob(job: Job, seed: Track): Promise<void> {
  const prev = folderJobs.get(job.folder) ?? Promise.resolve();
  const next = prev.then(() => runJobNow(job, seed));
  folderJobs.set(job.folder, next);
  return next;
}

const hasRunningJob = (folder: string) => [...jobs.values()].some((j) => j.folder === folder && (j.state === "running" || j.state === "finalizing"));

async function runJobNow(job: Job, seed: Track) {
  const crate = crateByFolder(job.folder);
  const existing = new Set(crate?.trackIds ?? []);
  const elsewhere = new Set(crates.filter((c) => c !== crate).flatMap((c) => c.trackIds));
  const queue = [...job.items];
  const worker = async () => {
    while (queue.length && job.state === "running") await processItem(job, queue.shift()!, existing, elsewhere);
  };
  await Promise.all(Array.from({ length: Math.max(1, settings.workers) }, worker));
  if (job.state === "cancelled") {
    for (const it of job.items) {
      if (it.status) continue;
      Object.assign(it, { status: "failed", reason: "cancelled", text: "cancelled" });
      emit({ type: "job", data: { job_id: job.job_id, track_id: it.track.id, text: "cancelled" } });
    }
    job.finished_at = new Date().toISOString();
    emit({ type: "job_state", data: { job_id: job.job_id, state: "cancelled" } });
    emit({ type: "job_done", data: clone(job) });
    return;
  }
  job.state = "finalizing";
  emit({ type: "job_state", data: { job_id: job.job_id, state: "finalizing" } });
  await sleep(1200);
  // library update: the crate now holds every file that exists
  const have = job.items.filter((i) => i.status === "downloaded" || i.status === "copied" || i.status === "exists").map((i) => i.track);
  let c = crate;
  if (!c) {
    c = { folder: job.folder, seed, trackIds: [], updatedAt: "" };
    crates.unshift(c);
    analysis.set(c.folder, new Map());
  }
  const ids = new Set([...c.trackIds, ...have.map((t) => t.id)]);
  c.trackIds = orderSet(seed, [...ids].map((id) => track(id)!).filter(Boolean)).map((t) => t.id);
  touch(c);
  const states = analysis.get(c.folder)!;
  for (const t of have) if (!states.has(t.id)) states.set(t.id, "queued");
  job.state = "done";
  job.finished_at = new Date().toISOString();
  emit({ type: "job_state", data: { job_id: job.job_id, state: "done" } });
  emit({ type: "job_done", data: clone(job) });
  void runAnalyses(c.folder);
}

// ---------------------------------------------------------------------------------------------------- search

function searchPage(term: string, page: number) {
  const q = term.trim().toLowerCase();
  if (!q) return { page, total_count: 0, has_next: false, items: [] as Track[] };
  const id = /([A-Za-z0-9]{22})/.exec(term)?.[1];
  if (id && POOL.has(id)) return { page: 1, total_count: 1, has_next: false, items: [POOL.get(id)!] };
  const tokens = q.split(/\s+/);
  if (tokens.every((t) => "fisher favour".includes(t)) && tokens.some((t) => t.length > 2)) {
    // the captured tunebat response for "fisher favour" (page 1); page 2 = tunebat recs
    if (page === 1) return { ...SEARCH_PAGE, has_next: true };
    if (page === 2) return { page: 2, total_count: SEARCH_PAGE.total_count, has_next: false, items: [...POOL.values()].filter((t) => !SEARCH_PAGE.items.some((s) => s.id === t.id)) };
    return { page, total_count: SEARCH_PAGE.total_count, has_next: false, items: [] };
  }
  const text = (t: Track) => `${t.display_name} ${t.album}`.toLowerCase();
  const all = [...POOL.values()].filter((t) => tokens.every((tok) => text(t).includes(tok)));
  const any = [...POOL.values()].filter((t) => !all.includes(t) && tokens.some((tok) => tok.length > 2 && text(t).includes(tok)));
  const items = [...all, ...any];
  return { page, total_count: items.length, has_next: page * 50 < items.length, items: items.slice((page - 1) * 50, page * 50) };
}

// ---------------------------------------------------------------------------------------------------- router

type Handler = (m: string[], body: Record<string, unknown>, q: URLSearchParams) => Resp | Promise<Resp>;
const routes: [Method, RegExp, Handler][] = [];
const on = (method: Method, pattern: string, h: Handler) => routes.push([method, new RegExp(`^${pattern}$`), h]);

on("GET", "/api/health", () => ok({ ok: true }));
on("GET", "/api/system", () => ok(system));
on("POST", "/api/system/update-ytdlp", async () => {
  await sleep(2600);
  const old = system.ytdlp_version;
  const latest = system.ytdlp_latest;
  if (latest && latest !== old) {
    system.ytdlp_latest = null; // installed; used after the next launch
    return ok({ ok: true, version: latest, message: `Installed yt-dlp ${latest} (running ${old} until restart)` });
  }
  return ok({ ok: true, version: old, message: `yt-dlp ${old} is already the latest version` });
});

on("GET", "/api/settings", () => ok(settings));
// the real server answers 501 outside the native window; the mock simulates the dialog picking a folder
on("POST", "/api/system/pick-folder", async () => {
  await sleep(700);
  return ok({ path: `${HOME}/Music/DJ Crates` });
});
on("PUT", "/api/settings", (_m, body) => {
  const patch = body as Partial<Settings>;
  if (patch.output_root !== undefined) {
    const root = String(patch.output_root).trim().replace(/^~(?=\/|$)/, HOME);
    if (!root.startsWith("/")) return fail(422, "output_root must be an absolute path");
    patch.output_root = root.replace(/\/+$/, "");
  }
  if (patch.workers !== undefined && !(patch.workers >= 1 && patch.workers <= 8)) return fail(422, "workers must be 1-8");
  Object.assign(settings, patch);
  system.output_root = settings.output_root;
  return ok(settings);
});

on("GET", "/api/search", async (_m, _b, q) => {
  await sleep(350);
  return ok(searchPage(q.get("term") ?? "", Number(q.get("page") ?? 1) || 1));
});

on("GET", "/api/crates/new/([^/]+)", async ([seedId]) => {
  await sleep(650);
  const seed = track(seedId);
  if (!seed) return fail(404, `Track ${seedId} not found on tunebat`);
  const folder = sanitize(seed.display_name);
  const existing = crateByFolder(folder);
  const path = crateDir(folder);
  const recs = recsFor(seed, settings.bpm_tolerance, new Set(existing?.trackIds ?? []), path, settings.pretick_all);
  const crate: Crate = { folder, path, seed, seed_existing_path: existing?.trackIds.includes(seed.id) ? `${path}/${fileName(seed)}` : null, recs };
  const order = [seed.id, ...recs.filter((r) => r.score.passed).map((r) => r.track.id), ...recs.filter((r) => !r.score.passed).map((r) => r.track.id)];
  setTimeout(() => preload(order), 50);
  return ok(crate);
});

on("GET", "/api/previews/([^/]+)", ([id]) => ok(previewStatus(id)));
on("POST", "/api/previews/([^/]+)/prioritize", ([id]) => {
  const s = previewStatus(id);
  if (s.state === "ready" || s.state === "no_match" || s.state === "loading") return ok(s);
  previewQueue = previewQueue.filter((x) => x !== id);
  startPreview(id); // "start now", even past the concurrency limit
  return ok(previewStatus(id));
});

on("POST", "/api/jobs", (_m, body) => {
  const seed = track(String(body.seed_id));
  if (!seed) return fail(404, "Unknown seed");
  const ids = (body.track_ids as string[]).filter((id) => POOL.has(id));
  if (!ids.length) return fail(422, "No tracks to download");
  const job: Job = {
    job_id: `job-${++jobSeq}-${Date.now().toString(36)}`,
    folder: sanitize(seed.display_name),
    state: "running",
    items: ids.map((id) => ({ track: track(id)!, text: "queued", status: null, reason: null, genre: null, path: null, youtube_url: null })),
    started_at: new Date().toISOString(),
    finished_at: null,
  };
  jobs.set(job.job_id, job);
  void runJob(job, seed);
  return ok(job);
});
on("GET", "/api/jobs", () => ok([...jobs.values()]));
on("GET", "/api/jobs/([^/]+)", ([id]) => (jobs.has(id) ? ok(jobs.get(id)) : fail(404, "Unknown job")));
on("DELETE", "/api/jobs/([^/]+)", ([id]) => {
  const job = jobs.get(id);
  if (!job) return fail(404, "Unknown job");
  if (job.state === "running") job.state = "cancelled";
  return ok(job);
});

on("GET", "/api/crates", () => ok([...crates].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).map((c) => crateSummary(c, settings.output_root))));

on("GET", "/api/crates/([^/]+)/set", ([folder]) => {
  const c = crateByFolder(folder);
  if (!c) return fail(404, `No crate "${folder}"`);
  setTimeout(() => void runAnalyses(folder), 1500);
  return ok(crateSet(c));
});
on("PUT", "/api/crates/([^/]+)/order", ([folder], body) => {
  const c = crateByFolder(folder);
  if (!c) return fail(404, `No crate "${folder}"`);
  if (hasRunningJob(folder)) return fail(409, "This crate has a running download job — try again when it finishes");
  const ids = body.track_ids as string[];
  if (ids.length !== c.trackIds.length || !ids.every((id) => c.trackIds.includes(id))) return fail(422, "track_ids must be a permutation of the crate's tracks");
  c.trackIds = [...ids];
  touch(c);
  setTimeout(() => emit({ type: "set_changed", data: { folder } }), 20);
  return ok(crateSet(c));
});
on("POST", "/api/crates/([^/]+)/auto-order", async ([folder]) => {
  const c = crateByFolder(folder);
  if (!c) return fail(404, `No crate "${folder}"`);
  if (hasRunningJob(folder)) return fail(409, "This crate has a running download job — try again when it finishes");
  await sleep(450);
  c.trackIds = orderSet(c.seed, c.trackIds.map((id) => track(id)!)).map((t) => t.id);
  touch(c);
  setTimeout(() => emit({ type: "set_changed", data: { folder } }), 20);
  return ok(crateSet(c));
});
on("GET", "/api/crates/([^/]+)/partners/([^/]+)", ([folder, id], _b, q) => {
  const c = crateByFolder(folder);
  const me = track(id);
  if (!c || !me) return fail(404, "Not found");
  const dir = q.get("direction") === "in" ? "in" : "out";
  const states = analysis.get(folder);
  const an = (t: Track) => (states?.get(t.id) === "ready" ? mockAnalysis(t) : null);
  const partners: Partner[] = c.trackIds
    .filter((x) => x !== id)
    .map((x) => track(x)!)
    .map((t) => ({ track: t, direction: dir, transition: dir === "out" ? mockTransition(me, an(me), t, an(t)) : mockTransition(t, an(t), me, an(me)) }));
  return ok(partners.sort((a, b) => b.transition.score - a.transition.score).slice(0, 8));
});
// ---- v4 mix plans + renders (mock/mixplan.ts; added by FRONTEND-SET)
const savedPlans = new Map<string, MixPlan>();
const renderedIds = new Set<string>();
function mixPair(folder: string, from: string, to: string): { c: MockCrate; a: Track; b: Track } | Resp {
  const c = crateByFolder(folder);
  const a = track(from);
  const b = track(to);
  if (!c || !a || !b || !c.trackIds.includes(from) || !c.trackIds.includes(to)) return fail(404, "track not in this crate");
  const st = analysis.get(folder);
  if (st?.get(from) !== "ready" || st?.get(to) !== "ready") return fail(409, "both tracks need their analysis first");
  return { c, a, b };
}
function genPlan(folder: string, a: Track, b: Track, technique?: string | null, bars?: number | null, style?: string | null, swap?: string | null): MixPlan {
  const t = mockTransition(a, mockAnalysis(a), b, mockAnalysis(b), settings.bpm_tolerance);
  return generateMixPlan(folder, a, mockAnalysis(a), b, mockAnalysis(b), t, technique, bars, style, swap);
}
on("GET", "/api/crates/([^/]+)/stems", ([folder]) => {
  const c = crates.find((x) => x.folder === folder);
  if (!c) return fail(404, "no such crate");
  const ids = c.trackIds;
  const tracks = Object.fromEntries(ids.map((id, i) => [id, i === ids.length - 1 ? { state: "running" as const, progress: 0.4, error: null } : { state: "ready" as const, progress: 1, error: null }]));
  return ok({ available: true, reason: null, tracks, ready: ids.length - 1, total: ids.length });
});
on("GET", "/api/crates/([^/]+)/mix/([^/]+)/([^/]+)", ([folder, from, to], _b, q) => {
  const p = mixPair(folder, from, to);
  if ("status" in p) return p;
  const technique = q.get("technique");
  const bars = Number(q.get("bars")) || null;
  const style = q.get("style");
  const swap = q.get("swap");
  const saved = savedPlans.get(`${folder}|${from}|${to}`);
  if (saved && !technique && !bars && !style && !swap) return ok(saved);
  return ok(genPlan(folder, p.a, p.b, technique, bars, style, swap));
});
on("PUT", "/api/crates/([^/]+)/mix/([^/]+)/([^/]+)", ([folder, from, to], body) => {
  const p = mixPair(folder, from, to);
  if ("status" in p) return p;
  const plan = { ...validateMixPlan(body as unknown as MixPlan), edited: true };
  savedPlans.set(`${folder}|${from}|${to}`, plan);
  return ok(plan);
});
on("DELETE", "/api/crates/([^/]+)/mix/([^/]+)/([^/]+)", ([folder, from, to]) => {
  const p = mixPair(folder, from, to);
  if ("status" in p) return p;
  savedPlans.delete(`${folder}|${from}|${to}`);
  return ok(genPlan(folder, p.a, p.b));
});
on("POST", "/api/crates/([^/]+)/mix/([^/]+)/([^/]+)/render", async ([folder, from, to], body) => {
  const p = mixPair(folder, from, to);
  if ("status" in p) return p;
  const plan = body && "tempo" in body ? validateMixPlan(body as unknown as MixPlan) : (savedPlans.get(`${folder}|${from}|${to}`) ?? genPlan(folder, p.a, p.b));
  const r = mockRender(plan);
  await emitRenderProgress(emit, r, folder, from, to, renderedIds.has(r.render_id));
  renderedIds.add(r.render_id);
  return ok(r);
});
// ---- full set mixes (mock: animated progress, then a synthesised mix you can play)
const setMixes = new Map<string, SetMix>();
const mixById = new Map<string, SetMix>();
function setMixTracks(target: string): { title: string; tracks: Track[]; folders: string[] } | null {
  const [kind, key] = [target.slice(0, target.indexOf(":")), target.slice(target.indexOf(":") + 1)];
  if (kind === "crate") {
    const c = crateByFolder(key);
    return c ? { title: c.seed?.display_name ?? c.folder, tracks: c.trackIds.map((id) => track(id)!).filter(Boolean), folders: c.trackIds.map(() => c.folder) } : null;
  }
  const sl = setlistStore.get(key);
  if (!sl) return null;
  const seen = new Set<string>();
  const tracks: Track[] = [];
  const folders: string[] = [];
  for (const f of sl.crates) for (const id of crateByFolder(f)?.trackIds ?? []) if (!seen.has(id) && track(id)) (seen.add(id), tracks.push(track(id)!), folders.push(f));
  return { title: sl.name, tracks, folders };
}
async function runSetMix(m: SetMix, tracks: Track[], folders: string[]) {
  const step = (patch: Partial<SetMix>) => {
    Object.assign(m, patch);
    emit({ type: "setmix", data: clone(m) });
  };
  step({ state: "preparing", message: "Collecting the tracks" });
  await sleep(500);
  for (let i = 0; i < tracks.length; i++) {
    if (m.state === "cancelled") return;
    step({ state: "rendering", progress: 0.05 + (0.93 * i) / tracks.length, message: `Track ${i + 1}/${tracks.length} · ${tracks[i].display_name}` });
    await sleep(450);
  }
  if (m.state === "cancelled") return;
  let t = 0;
  const out = tracks.map((tr, i) => {
    const start = t;
    t += Math.max(60, tr.duration_s - 32);
    return { index: i, track_id: tr.id, display_name: tr.display_name, name: tr.name, artists: tr.artists, camelot: tr.camelot, bpm: tr.bpm, folder: folders[i], start_s: start, in_s: Math.max(0, start - 30), out_s: t + 30 };
  });
  step({
    state: "done",
    progress: 1,
    message: "Done",
    finished_at: new Date().toISOString(),
    result: {
      audio_url: `/api/setmix/${m.mix_id}/audio`,
      path: `${settings.output_root}/Mixes/${sanitize(m.title)}/${sanitize(m.title)} - 2026-10-03 2200.mp3`,
      cue_path: "",
      txt_path: "",
      format: "mp3",
      duration_s: t + 30,
      lufs: -11.4,
      true_peak_db: -1.6,
      tracks: out,
      transitions: out.slice(1).map((x, i) => {
        const style = mockStyle("phrase blend", x.track_id);
        const technique = style === "quick cut" ? "quick cut" : style === "double drop" ? "double drop" : style === "filter fade" ? "filter sweep" : "bass swap";
        return { index: i, technique, style, bars: 16, overlap: hash(x.track_id) % 3 ? 16 : 0, at_s: x.in_s, edited: i === 1 };
      }),
      warnings: [],
    },
  });
}
function startSetMix(target: string) {
  const live = setMixes.get(target);
  if (live && (live.state === "queued" || live.state === "preparing" || live.state === "rendering")) return ok(live);
  const src = setMixTracks(target);
  if (!src) return fail(404, "Not found");
  if (src.tracks.length < 2) return fail(422, "a set mix needs at least two tracks");
  const m: SetMix = { mix_id: Math.random().toString(16).slice(2, 14).padEnd(12, "0"), target, title: src.title, state: "queued", progress: 0, message: "Waiting for another mix to finish", error: null, started_at: new Date().toISOString(), finished_at: null, result: null };
  setMixes.set(target, m);
  mixById.set(m.mix_id, m);
  void runSetMix(m, src.tracks, src.folders);
  return ok(m);
}
on("POST", "/api/crates/([^/]+)/setmix", ([folder]) => startSetMix(`crate:${folder}`));
on("GET", "/api/crates/([^/]+)/setmix", ([folder]) => ok({ mix: setMixes.get(`crate:${folder}`) ?? null, stale: false }));
on("POST", "/api/setlists/([^/]+)/setmix", ([id]) => startSetMix(`setlist:${id}`));
on("GET", "/api/setlists/([^/]+)/setmix", ([id]) => ok({ mix: setMixes.get(`setlist:${id}`) ?? null, stale: false }));
on("GET", "/api/setmix/([^/]+)", ([id]) => (mixById.has(id) ? ok(mixById.get(id)) : fail(404, "no such mix")));
on("DELETE", "/api/setmix/([^/]+)", ([id]) => {
  const m = mixById.get(id);
  if (!m) return fail(404, "no such mix");
  if (m.state !== "done") {
    Object.assign(m, { state: "cancelled", message: "Cancelled", finished_at: new Date().toISOString() });
    emit({ type: "setmix", data: clone(m) });
  }
  return ok(m);
});

// ---- setlists + crate links
interface MockSetlist {
  id: string;
  name: string;
  crates: string[];
  created_at: string;
  updated_at: string;
}
const setlistStore = new Map<string, MockSetlist>();
function link(fromFolder: string, toFolder: string): CrateLink | null {
  const a = crateByFolder(fromFolder);
  const b = crateByFolder(toFolder);
  if (!a?.trackIds.length || !b?.trackIds.length) return null;
  const ta = track(a.trackIds[a.trackIds.length - 1])!;
  const tb = track(b.trackIds[0])!;
  const an = (f: string, t: Track) => (analysis.get(f)?.get(t.id) === "ready" ? mockAnalysis(t) : null);
  const tr = mockTransition(ta, an(fromFolder, ta), tb, an(toFolder, tb), settings.bpm_tolerance);
  let better: CrateLink["better_entry"] = null;
  for (const id of b.trackIds.slice(1)) {
    const t2 = mockTransition(ta, an(fromFolder, ta), track(id)!, an(toFolder, track(id)!), settings.bpm_tolerance);
    if (t2.score >= tr.score + 8 && (!better || t2.score > better.transition.score)) better = { track: track(id)!, transition: t2 };
  }
  return { from_folder: fromFolder, to_folder: toFolder, from_track: ta, to_track: tb, transition: tr, better_entry: better, from_seed: a.seed ?? null, to_seed: b.seed ?? null };
}
function setlistJson(sl: MockSetlist): Setlist {
  const seen = new Set<string>();
  const blocks = sl.crates.map((f) => {
    const c = crateByFolder(f);
    if (!c) return { folder: f, seed: null, missing: true, tracks: [] };
    const set = crateSet(c);
    const tracks = set.tracks.filter((t) => !seen.has(t.track.id) && (seen.add(t.track.id), true));
    return { folder: f, seed: c.seed ?? null, missing: false, tracks };
  });
  const live = blocks.filter((b) => b.tracks.length);
  const links = live.slice(1).map((b, i) => link(live[i].folder, b.folder)).filter((l): l is CrateLink => !!l);
  return { ...sl, blocks, links, track_count: blocks.reduce((n, b) => n + b.tracks.length, 0), duration_s: blocks.reduce((n, b) => n + b.tracks.reduce((m, t) => m + t.track.duration_s, 0), 0) };
}
const touchSetlist = (sl: MockSetlist) => {
  sl.updated_at = new Date().toISOString();
  setTimeout(() => emit({ type: "setlist_changed", data: { id: sl.id } }), 20);
};
on("GET", "/api/crates/([^/]+)/links", ([folder]) => {
  if (!crateByFolder(folder)) return fail(404, "Not found");
  const others = crates.filter((c) => c.folder !== folder);
  const by = (l: CrateLink) => -l.transition.score;
  return ok({
    out: others.map((c) => link(folder, c.folder)).filter((l): l is CrateLink => !!l).sort((x, y) => by(x) - by(y)).slice(0, 6),
    in: others.map((c) => link(c.folder, folder)).filter((l): l is CrateLink => !!l).sort((x, y) => by(x) - by(y)).slice(0, 6),
  });
});
on("GET", "/api/setlists", () =>
  ok([...setlistStore.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).map((sl) => ({ id: sl.id, name: sl.name, crates: sl.crates, seeds: sl.crates.map((f) => crateByFolder(f)?.seed ?? null), track_count: sl.crates.reduce((n, f) => n + (crateByFolder(f)?.trackIds.length ?? 0), 0), updated_at: sl.updated_at }))),
);
on("POST", "/api/setlists", (_m, body) => {
  const cr = ((body.crates as string[]) ?? []).filter((f) => crateByFolder(f));
  if (!cr.length) return fail(422, "pick at least one crate");
  const now = new Date().toISOString();
  const name = typeof body.name === "string" && body.name.trim() ? body.name.trim() : cr.slice(0, 2).map((f) => crateByFolder(f)?.seed?.name ?? f).join(" → ") + (cr.length > 2 ? " …" : "");
  const sl: MockSetlist = { id: Math.random().toString(16).slice(2, 14).padEnd(12, "0"), name, crates: cr, created_at: now, updated_at: now };
  setlistStore.set(sl.id, sl);
  touchSetlist(sl);
  return ok(setlistJson(sl));
});
on("GET", "/api/setlists/([^/]+)", ([id]) => (setlistStore.has(id) ? ok(setlistJson(setlistStore.get(id)!)) : fail(404, "no such setlist")));
on("PUT", "/api/setlists/([^/]+)", ([id], body) => {
  const sl = setlistStore.get(id);
  if (!sl) return fail(404, "no such setlist");
  if (typeof body.name === "string") sl.name = body.name;
  if (Array.isArray(body.crates)) sl.crates = (body.crates as string[]).filter((f) => crateByFolder(f));
  touchSetlist(sl);
  return ok(setlistJson(sl));
});
on("DELETE", "/api/setlists/([^/]+)", ([id]) => {
  setlistStore.delete(id);
  emit({ type: "setlist_changed", data: { id } });
  return ok({ ok: true });
});
on("POST", "/api/setlists/([^/]+)/auto-order", async ([id]) => {
  const sl = setlistStore.get(id);
  if (!sl) return fail(404, "no such setlist");
  await sleep(400);
  const [first, ...rest] = sl.crates;
  const order = [first];
  const left = new Set(rest);
  while (left.size) {
    const nxt = [...left].sort((x, y) => (link(order[order.length - 1], y)?.transition.score ?? 0) - (link(order[order.length - 1], x)?.transition.score ?? 0))[0];
    order.push(nxt);
    left.delete(nxt);
  }
  sl.crates = order;
  touchSetlist(sl);
  return ok(setlistJson(sl));
});
const linkPlans = new Map<string, MixPlan>();
function linkPair(id: string, from: string, to: string): { a: Track; b: Track } | Resp {
  const sl = setlistStore.get(id);
  const a = track(from);
  const b = track(to);
  if (!sl || !a || !b) return fail(404, "track not in this setlist");
  return { a, b };
}
on("GET", "/api/setlists/([^/]+)/mix/([^/]+)/([^/]+)", ([id, from, to], _b, q) => {
  const p = linkPair(id, from, to);
  if ("status" in p) return p;
  const saved = linkPlans.get(`${id}|${from}|${to}`);
  if (saved && !q.get("technique") && !q.get("bars") && !q.get("style") && !q.get("swap")) return ok(saved);
  return ok(genPlan(`setlist:${id}`, p.a, p.b, q.get("technique"), Number(q.get("bars")) || null, q.get("style"), q.get("swap")));
});
on("PUT", "/api/setlists/([^/]+)/mix/([^/]+)/([^/]+)", ([id, from, to], body) => {
  const p = linkPair(id, from, to);
  if ("status" in p) return p;
  const plan = { ...validateMixPlan(body as unknown as MixPlan), edited: true };
  linkPlans.set(`${id}|${from}|${to}`, plan);
  return ok(plan);
});
on("DELETE", "/api/setlists/([^/]+)/mix/([^/]+)/([^/]+)", ([id, from, to]) => {
  const p = linkPair(id, from, to);
  if ("status" in p) return p;
  linkPlans.delete(`${id}|${from}|${to}`);
  return ok(genPlan(`setlist:${id}`, p.a, p.b));
});
on("POST", "/api/setlists/([^/]+)/mix/([^/]+)/([^/]+)/render", async ([id, from, to], body) => {
  const p = linkPair(id, from, to);
  if ("status" in p) return p;
  const plan = body && "tempo" in body ? validateMixPlan(body as unknown as MixPlan) : (linkPlans.get(`${id}|${from}|${to}`) ?? genPlan(`setlist:${id}`, p.a, p.b));
  const r = mockRender(plan);
  await emitRenderProgress(emit, r, `setlist:${id}`, from, to, renderedIds.has(r.render_id));
  renderedIds.add(r.render_id);
  return ok(r);
});

on("GET", "/api/analysis/([^/]+)/([^/]+)", async ([folder, id]) => {
  const t = track(id);
  const st = analysis.get(folder)?.get(id);
  if (!t || !st) return fail(404, "Not found");
  if (st === "error") return fail(422, "analysis failed: could not decode audio (ffmpeg exit 1)");
  if (st !== "ready") return fail(404, `analysis ${st === "missing" ? "queued" : st}`);
  await sleep(120);
  return ok(mockAnalysis(t));
});

on("POST", "/api/reveal", (_m, body) => {
  const path = String(body.path ?? "");
  if (!path.startsWith(settings.output_root)) return fail(400, "path must be inside the crates folder");
  setTimeout(() => emit({ type: "toast", data: { level: "info", message: `Mock: would reveal ${path.replace(HOME, "~")} in Finder` } }), 30);
  return ok({ ok: true });
});

/** Handles one API call (same signature the real transport resolves to). */
export async function mockFetch(method: Method, path: string, body?: unknown): Promise<Resp> {
  const [p, qs] = path.split("?");
  const query = new URLSearchParams(qs ?? "");
  await sleep(between(Math.random, 60, 180));
  for (const [m, re, h] of routes) {
    if (m !== method) continue;
    const match = re.exec(p);
    if (match) {
      try {
        return await h(match.slice(1).map(decodeURIComponent), (body ?? {}) as Record<string, unknown>, query);
      } catch (e) {
        return fail(500, `mock error: ${(e as Error).message}`);
      }
    }
  }
  return fail(404, `Not found: ${method} ${p}`);
}

/** For tests/dev tools: the Favour seed id (the richest fixture). */
export const MOCK_SEED_ID = FAVOUR.id;
