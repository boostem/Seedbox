// Visual regression tests: screenshots every route and its key states at the two reference sizes and compares them
// with the baselines in visual-baselines/ (DESIGN.md "Screenshots").
//
//   deno task screenshot [--no-build] [--only=NAME,…] [--no-docker]
//   UPDATE_SCREENSHOTS=1 deno task screenshot     accepts the new screenshots as baselines
//
// Builds dist/ (unless --no-build), then runs itself in the Docker image of scripts/screenshot.Dockerfile (a pinned
// Chrome and fonts, so every machine renders the same pixels). There it serves dist/ with a stub /api, drives
// Chrome over the DevTools protocol with animations off, and writes screenshots/actual/, plus diff/ and expected/
// for each screenshot that changed (git-ignored). Exits 1 when one changed, is new or has no baseline.
// --no-docker uses a Chrome on this machine ($CHROME, Playwright's headless shell, chromium or Chrome): quick to
// look at, but its fonts won't match the baselines.

import { decodePng, diffImages, encodePng } from "./png.ts";

const ROOT = new URL("..", import.meta.url).pathname;
const DIST = `${ROOT}dist`;
const BASELINES = `${ROOT}visual-baselines`;
const OUT = `${ROOT}screenshots`;
const IMAGE = "seedbox2-screenshots";
const SIZES = [[1440, 900], [1280, 720]] as const;
/** A pixel has changed when a channel moved by more than this (0-255)… */
const PIXEL_THRESHOLD = 24;
/** …and a screenshot has changed when more pixels than this did. The image renders the same pixels every time, so
 * this only absorbs stray anti-aliasing: a changed word is about 70 pixels. */
const MAX_CHANGED = 16;

const LISTEN_RATE = (name: string, wins: number, trials: number, low: number, high: number) => ({
  name, wins, trials, win_rate: wins / trials, ci_low: low, ci_high: high, scores: { smoothness: 3.8, energy: 3.4, dj: 3.1 },
});
const LISTEN_SUMMARY = {
  ratings: 42,
  raters: 3,
  versions: [LISTEN_RATE("seedbox2-2.0.0", 27, 34, 0.63, 0.9), LISTEN_RATE("human", 9, 20, 0.26, 0.66), LISTEN_RATE("seedbox1-0.3.0", 6, 30, 0.1, 0.38)],
  styles: [LISTEN_RATE("bass swap", 15, 24, 0.43, 0.79), LISTEN_RATE("echo out", 9, 24, 0.21, 0.57)],
  matchups: [{ name: "seedbox1-0.3.0", opponent: "seedbox2-2.0.0", wins: 4, trials: 24, win_rate: 1 / 6, ci_low: 0.07, ci_high: 0.36 }],
  slot_a: { wins: 22, trials: 42, win_rate: 0.52, ci_low: 0.38, ci_high: 0.67 },
};
const LISTEN_STATUS = { folder: "/home/dj/.local/share/seedbox2/listening", pairs: 6, renders: 18, comparisons: 18, ratings: 42, progress: { done: 3, total: 18 } };
const LISTEN_EMPTY = { ...LISTEN_STATUS, pairs: 0, renders: 0, comparisons: 0, ratings: 0, progress: { done: 0, total: 0 } };

/** What the stub /api answers: `live` an engine that works, `loading` one that never answers, `offline` none. */
type ApiState = "live" | "loading" | "offline";

interface Shot {
  name: string;
  hash: string;
  api?: ApiState;
  /** Keys pressed after the page settled (DevTools key names; "Mod" is Ctrl). */
  keys?: string[];
  /** How long the page settles before the screenshot (ms). */
  settle?: number;
  /** GET responses that differ from `API` for this shot, by path. */
  data?: Record<string, unknown>;
  /** GET paths that answer 500 for this shot. */
  fail?: string[];
}

const SYSTEM = {
  app: "Seedbox 2",
  version: "2.0.0-dev.0",
  python: "3.12.8",
  platform: "linux",
  machine: "x86_64",
  frozen: false,
  ffmpeg: "/usr/bin/ffmpeg",
  deno: "/usr/local/bin/deno",
  frontend: "/opt/seedbox2/frontend2/dist",
  folders: {
    settings: "/home/dj/.local/share/seedbox2",
    cache: "/home/dj/.cache/seedbox2",
    logs: "/home/dj/.local/state/seedbox2/log",
    music: "/home/dj/Music/MusicReco",
  },
  reveal_label: "Show in file manager",
};

const LIBRARY = { path: "/home/dj/.local/share/seedbox2/library.sqlite3", tracks: 12, track_paths: 12, crates: 2, sets: 1 };

const TRACKS = [
  ["Losing It", "FISHER", "Losing It", 233, 125, "mp3", "rekordbox"],
  ["Favour", "FISHER, Tones And I", "Favour", 191, 126, "flac", "local"],
  ["Rumble", "Skrillex, Fred again.., Flowdan", "Rumble", 160, 140, "aiff", "local"],
  ["Turn On The Lights again..", "Fred again.., Swedish House Mafia", "Turn On The Lights again..", 222, 128, "wav", "itunes"],
  ["Shiver", "John Summit, HAYLA", "Shiver", 199, 126, "mp3", "local"],
  ["Where You Are", "John Summit, HAYLA", "Where You Are", 221, 126, "mp3", "rekordbox"],
  ["Stay With Me", "Calvin Harris, Justin Timberlake, Halsey, Pharrell", "Stay With Me", 213, 125, "m4a", "itunes"],
  ["Body Language", "Dom Dolla", "Body Language", 365, 124, "flac", "local"],
  ["Miracle Maker", "Dom Dolla, Clementine Douglas", "Miracle Maker", 168, 126, "mp3", "seedbox1"],
  ["Eat Your Man", "Dom Dolla, Nelly Furtado", "Eat Your Man", 172, 128, "mp3", "seedbox1"],
  ["Children", "Robert Miles", "Dreamland", 459, 137, "flac", "local"],
  ["Strings of Life", "Rhythim Is Rhythim", "Strings of Life", 371, 124, "wav", "rekordbox"],
].map(([title, artist, album, duration, bpm, format, source], i) => ({
  hash: `${i}`.padStart(40, "0"),
  title,
  artist,
  album,
  duration,
  tags: { file: { bpm, format } },
  source,
  added_at: "2026-10-01T12:00:00Z",
}));

type RowState = "new" | "queued" | "analysing" | "done" | "failed";

/** The library view's rows: `analysed` of them done, the rest in the `rest` states in turn, with what they carry. */
function rows(analysed: number, rest: RowState[] = []) {
  return TRACKS.map((t, i) => {
    const state: RowState = i < analysed ? "done" : rest[(i - analysed) % rest.length];
    const done = state === "done";
    return {
      hash: t.hash, duration: t.duration, title: t.title, artist: t.artist, album: t.album, source: t.source, added_at: t.added_at,
      bpm: done ? t.tags.file.bpm : null, bpm_from: done ? "analysis" : null,
      // #45: a key we aren't sure of shows as "?", and a tag that disagrees sits next to ours
      key: done && i % 5 !== 3 ? `${(i % 12) + 1}A` : null, key_from: !done ? null : i % 5 === 3 ? "unknown" : "analysis",
      key_hint: done && i % 4 === 1 ? "3B" : null, key_hint_from: done && i % 4 === 1 ? "tunebat" : null,
      energy: done ? 0.4 + (i % 6) / 10 : null, energy_from: done ? "analysis" : null,
      genre: done ? "House" : "", text: "", artwork: false, format: t.tags.file.format,
      status: { state, waveform: done, stems: false, stems_progress: null, analyses: [], jobs: state === "failed" ? { waveform: "failed" } : {} },
    };
  });
}

const model = (over: Record<string, unknown> = {}) => ({
  name: "beat_this", version: "final0", title: "Beat This!", file: "beat_this.onnx", url: "https://example.org/beat_this.onnx", sha256: "0", size: 97_000_000,
  licence: "MIT", attribution: "Foscarin, Schlüter and Widmer, ISMIR 2024.", homepage: "https://github.com/CPJKU/beat_this", used_for: "Beats and downbeats",
  fallback: "the Seedbox 1 beat tracker", inputs: "log-mel", state: "installed", installed: true, path: "/home/dj/.cache/seedbox2/models/beat_this/final0/beat_this.onnx", done: 97_000_000, error: null, ...over,
});

function modelsStatus(m: ReturnType<typeof model>) {
  return {
    dir: "/home/dj/.cache/seedbox2/models", models: [m], missing: m.installed ? [] : [m], offline: !m.installed, total_bytes: m.size, installed_bytes: m.installed ? m.size : 0,
    settings: { accelerator: false, auto_download: true },
  };
}
const MODEL_MISSING = { state: "missing", installed: false, path: null, done: 0 };
const POWER = { mode: "auto", low_power: false, reason: null, on_battery: false, battery_percent: 100, thermal: "nominal", limits: { stems_paused: false, analysis_paused: false, light_workers: 6, light_workers_full: 6, fps: null } };
const ON_BATTERY = { ...POWER, low_power: true, reason: "battery", on_battery: true, battery_percent: 64, limits: { ...POWER.limits, stems_paused: true, light_workers: 3, fps: 30 } };
const FIRST_RUN = { "/api/onboarding": { first_run: true }, "/api/library/view": { total: 0, items: [] } };

const sessionTrack = (hash: string, title: string) => ({ hash, title, artist: "Someone", duration: 240, grid: null, sections: [], key: { key: null, camelot: null }, integrated_lufs: null });
const TRACK_A = sessionTrack("a", "Losing It");
const TRACK_B = sessionTrack("b", "Favour");

/** What the stub /api answers to a GET in the `live` state (a shot's `data` overrides these). */
const SET_ARCS = [
  { name: "warm_up", label: "Warm-up", description: "Starts low and builds slowly", points: [[0, 0.05], [0.5, 0.4], [0.85, 0.7], [1, 0.8]] },
  { name: "peak_time", label: "Peak time", description: "Peaks about two thirds in", points: [[0, 0.55], [0.25, 0.8], [0.7, 1], [0.9, 0.95], [1, 0.75]] },
  { name: "sunset", label: "Sunset", description: "Comes down to a quiet close", points: [[0, 0.8], [0.35, 0.6], [0.75, 0.3], [1, 0.1]] },
  { name: "rollercoaster", label: "Rollercoaster", description: "Waves of tension and release", points: [[0, 0.4], [0.2, 0.9], [0.4, 0.35], [0.6, 0.95], [0.8, 0.4], [1, 0.9]] },
  { name: "flat", label: "Flat", description: "Holds one level throughout", points: [[0, 0.5], [1, 0.5]] },
];
const SET_ORDER = {
  order: ([["Morning Light", "Halcyon Drive", 122, "8A", 4, 0], ["Glass Rooms", "Oda Mae", 123, "9A", 5, 0], ["Night Bus", "Kerrin", 124, "10A", 6, 0], ["Undertow", "Halcyon Drive", 125, "10A", 8, 0], ["Cold Harbour", "Saskia Lind", 126, "3A", 9, -1], ["Long Way Home", "Oda Mae", 127, "4B", 7, 0]] as const)
    .map(([title, artist, bpm, key, rating, shift], position) => ({ position, from_position: position, hash: `h${position}`, title, artist, bpm, key, energy: (rating - 1) / 9, rating, shift, pinned: position === 0, target_energy: 0.5, target_bpm: bpm })),
  transitions: [
    { from: "h0", to: "h1", text: "energy +1, 8A → 9A, same vibe", flags: [], cost: 0.8 },
    { from: "h1", to: "h2", text: "energy +1, 9A → 10A, same vibe", flags: [], cost: 0.7 },
    { from: "h2", to: "h3", text: "energy +2, 10A → 10A, related vibe", flags: [], cost: 1.1 },
    { from: "h3", to: "h4", text: "energy +1, 10A → 10A (3A -1 st), same vibe", flags: ["key_fix"], cost: 2.1 },
    { from: "h4", to: "h5", text: "energy -2, 10A (3A -1 st) → 4B, tempo +5.8 %, vibe shift", flags: ["key_clash", "tempo_wide"], cost: 5.3 },
  ],
  cost: 10.1,
  cost_before: 10.1,
  arc: SET_ARCS[1],
  tempo: { start: 122, end: 127, auto: true },
  unknown: { bpm: 0, key: 0, energy: 0, vibe: 2, vocals: 6, genre: 6 },
  settings: { arc: "peak_time", tempo_start: null, tempo_end: null, pins: [{ position: 0, hash: "h0" }] },
};
const API: Record<string, unknown> = {
  "/api/health": { ok: true },
  "/api/system": SYSTEM,
  "/api/library": LIBRARY,
  "/api/library/tracks": { total: TRACKS.length, items: TRACKS },
  "/api/library/folders": { folders: [{ path: "/home/dj/Music/Techno 2026", exists: true }, { path: "/media/usb/Old crates", exists: false }], scanning: null },
  "/api/library/import": { state: "idle" },
  "/api/listen/status": LISTEN_STATUS,
  "/api/listen/next": { id: "trial", pair: "House into house", progress: { done: 3, total: 18 } },
  "/api/listen/summary": LISTEN_SUMMARY,
  "/api/analysis/stems": { limit_gb: 20, format: "flac", low_power: false, usage: { tracks: 38, bytes: 2_874_000_000 } },
  "/api/library/view": { total: TRACKS.length, items: rows(TRACKS.length) },
  "/api/library/crates": [{ id: 1, name: "Peak time", folder: null, source: "local", track_count: 8 }, { id: 2, name: "Warm-up", folder: null, source: "local", track_count: 4 }],
  "/api/library/smart-crates": { crates: [] },
  "/api/sets": [{ id: 1, name: "Friday", crate_id: null, source: "local", track_count: 6, updated_at: "2026-10-01T12:00:00Z" }],
  "/api/sets/arcs": SET_ARCS,
  "/api/sets/1/order": SET_ORDER,
  "/api/models": modelsStatus(model()),
  "/api/onboarding": { first_run: false },
  "/api/power": POWER,
  "/api/mixes/master": { target: "streaming", target_lufs: -14, ceiling_dbtp: -1.6, makeup_db: 0.5, targets: { streaming: -14, club: -9 } },
};

// Every route, and the states each one has so far. Area tickets add their screens' states (empty, loading,
// analysing, error) here as they land.
const SHOTS: Shot[] = [
  { name: "home", hash: "#/" },
  { name: "home-loading", hash: "#/", api: "loading" },
  { name: "library", hash: "#/library" },
  { name: "library-empty", hash: "#/library", data: { "/api/library": { ...LIBRARY, tracks: 0, track_paths: 0, crates: 0 }, "/api/library/tracks": { total: 0, items: [] }, "/api/library/view": { total: 0, items: [] }, "/api/library/crates": [], "/api/sets": [] } },
  { name: "library-analysing", hash: "#/library", data: { "/api/library/view": { total: TRACKS.length, items: rows(4, ["analysing", "queued", "queued"]) } } },
  { name: "library-analysis-failed", hash: "#/library", data: { "/api/library/view": { total: TRACKS.length, items: rows(9, ["failed"]) } } },
  { name: "library-load-failed", hash: "#/library", fail: ["/api/library/view"] },
  { name: "ffmpeg-missing", hash: "#/", data: { "/api/system": { ...SYSTEM, ffmpeg: null, frozen: true } } },
  { name: "deno-missing", hash: "#/", data: { "/api/system": { ...SYSTEM, deno: null } } },
  { name: "onboarding-music", hash: "#/", data: { ...FIRST_RUN, "/api/models": modelsStatus(model(MODEL_MISSING)) } },
  { name: "onboarding-models", hash: "#/", keys: ["Enter"], data: { ...FIRST_RUN, "/api/models": modelsStatus(model(MODEL_MISSING)) } },
  { name: "onboarding-models-downloading", hash: "#/", keys: ["Enter"], data: { ...FIRST_RUN, "/api/models": modelsStatus(model({ ...MODEL_MISSING, state: "downloading", done: 41_000_000 })) } },
  { name: "onboarding-models-failed", hash: "#/", keys: ["Enter"], data: { ...FIRST_RUN, "/api/models": modelsStatus(model({ ...MODEL_MISSING, state: "failed", done: 41_000_000, error: "connection lost" })) } },
  { name: "onboarding-analysis", hash: "#/", keys: ["Enter", "Enter"], data: FIRST_RUN },
  { name: "library-loading", hash: "#/library", api: "loading" },
  { name: "library-importing", hash: "#/library", data: { "/api/library/import": { state: "running", kind: "files", label: "Techno 2026", done: 37, total: 120, file: "/home/dj/Music/Techno 2026/Track 38.flac" } } },
  { name: "library-import-failed", hash: "#/library", data: { "/api/library/import": { state: "failed", kind: "xml", label: "rekordbox.xml", error: "not a Rekordbox or iTunes XML file" } } },
  { name: "crates", hash: "#/crates" },
  { name: "sets", hash: "#/sets" },
  { name: "sets-empty", hash: "#/sets", data: { "/api/sets": [] } },
  { name: "sets-loading", hash: "#/sets", api: "loading" },
  { name: "sets-load-failed", hash: "#/sets", fail: ["/api/sets/1/order"] },
  { name: "discover", hash: "#/discover" },
  { name: "mixes", hash: "#/mixes" },
  { name: "settings", hash: "#/settings" },
  { name: "settings-low-power", hash: "#/settings", data: { "/api/power": ON_BATTERY } },
  { name: "settings-loading", hash: "#/settings", api: "loading" },
  { name: "settings-offline", hash: "#/settings", api: "offline", settle: 5000 }, // offline after 4 failed reconnects (~3.5 s)
  { name: "ui", hash: "#/ui" },
  { name: "bench", hash: "#/bench" },
  { name: "listen", hash: "#/listen" },
  { name: "now-playing", hash: "#/now-playing?demo=1" },
  { name: "now-playing-choose", hash: "#/now-playing" },
  { name: "now-playing-no-sets", hash: "#/now-playing", data: { "/api/sets": [] } },
  { name: "now-playing-no-plans", hash: "#/now-playing?set=1", data: { "/api/sets/1/session": { id: 1, name: "Friday", tracks: [TRACK_A, TRACK_B], plans: [null] } } },
  { name: "listen-results", hash: "#/listen?tab=results" },
  { name: "listen-no-renders", hash: "#/listen", data: { "/api/listen/status": LISTEN_EMPTY, "/api/listen/next": undefined } },
  { name: "listen-no-ratings", hash: "#/listen?tab=results", data: { "/api/listen/status": LISTEN_EMPTY, "/api/listen/summary": { ratings: 0, raters: 0, versions: [], styles: [], matchups: [], slot_a: { wins: 0, trials: 0, win_rate: null, ci_low: 0, ci_high: 1 } } } },
  { name: "not-found", hash: "#/no-such-page" },
  { name: "command-palette", hash: "#/", keys: ["Mod+k"] },
  { name: "shortcuts", hash: "#/", keys: ["?"] },
];

const flags = Deno.args.filter((a) => !a.startsWith("--only="));
const onlyArg = Deno.args.find((a) => a.startsWith("--only="));
const only = onlyArg ? new Set(onlyArg.slice("--only=".length).split(",")) : null;
const update = Deno.env.get("UPDATE_SCREENSHOTS") === "1";
const inDocker = Deno.env.get("SEEDBOX_SCREENSHOTS_IN_DOCKER") === "1";

async function run(cmd: string, args: string[], cwd = ROOT): Promise<void> {
  const { code } = await new Deno.Command(cmd, { args, cwd, stdout: "inherit", stderr: "inherit" }).output();
  if (code !== 0) throw new Error(`${cmd} ${args.join(" ")} exited with ${code}`);
}

async function exists(path: string): Promise<boolean> {
  try {
    await Deno.stat(path);
    return true;
  } catch {
    return false;
  }
}

/** Builds the image and runs this script in it, on this folder. */
async function runInDocker(): Promise<number> {
  await run("docker", ["build", "-q", "-t", IMAGE, "-f", `${ROOT}scripts/screenshot.Dockerfile`, `${ROOT}scripts`]);
  const uid = Deno.uid(), gid = Deno.gid();
  const { code } = await new Deno.Command("docker", {
    args: [
      "run", "--rm", "--init", "--ipc=host",
      ...(uid != null && gid != null ? ["-u", `${uid}:${gid}`] : []),
      "-v", `${ROOT}:/frontend2`, "-w", "/frontend2",
      "-e", "HOME=/tmp", "-e", "DENO_DIR=/tmp/deno", "-e", "DENO_NO_UPDATE_CHECK=1",
      "-e", `UPDATE_SCREENSHOTS=${update ? 1 : 0}`,
      IMAGE,
      "deno", "run", "--no-config", "--no-lock", "--allow-all", "scripts/screenshot.ts", ...Deno.args,
    ],
    stdout: "inherit",
    stderr: "inherit",
  }).output();
  return code;
}

async function findChrome(): Promise<string> {
  const env = Deno.env.get("CHROME");
  if (env) return env;
  const home = Deno.env.get("HOME") ?? "";
  const caches = [Deno.env.get("PLAYWRIGHT_BROWSERS_PATH"), `${home}/.cache/ms-playwright`, `${home}/Library/Caches/ms-playwright`];
  for (const cache of caches) {
    if (!cache) continue;
    try {
      for await (const e of Deno.readDir(cache)) {
        if (!e.name.startsWith("chromium_headless_shell-")) continue;
        for (const arch of ["linux64", "linux-arm64", "mac-arm64", "mac-x64"]) {
          const p = `${cache}/${e.name}/chrome-headless-shell-${arch}/chrome-headless-shell`;
          if (await exists(p)) return p;
        }
      }
    } catch {
      /* no playwright cache */
    }
  }
  for (const d of (Deno.env.get("PATH") ?? "").split(":")) {
    for (const name of ["chrome-headless-shell", "chromium", "chromium-browser", "google-chrome"]) {
      if (await exists(`${d}/${name}`)) return `${d}/${name}`;
    }
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (await exists(mac)) return mac;
  throw new Error("No Chrome found: set CHROME to a chrome-headless-shell or Chrome binary");
}

const TYPES: Record<string, string> = {
  html: "text/html; charset=utf-8",
  js: "text/javascript",
  css: "text/css",
  svg: "image/svg+xml",
  json: "application/json",
  png: "image/png",
  woff2: "font/woff2",
};

/** dist/ and a stub /api on a free port. `shot()` says how the API behaves for the page being shot. */
function serve(shot: () => Shot): { server: Deno.HttpServer<Deno.NetAddr>; close: () => void } {
  const hanging = new Set<() => void>();
  const hang = () =>
    new Promise<Response>((resolve) => {
      const done = () => {
        hanging.delete(done);
        resolve(new Response("closing", { status: 503 }));
      };
      hanging.add(done);
    });
  const server = Deno.serve({ hostname: "127.0.0.1", port: 0, onListen: () => {} }, async (req) => {
    let path = decodeURIComponent(new URL(req.url).pathname);
    if (path.startsWith("/api/")) {
      const s = shot().api ?? "live";
      const data = { ...API, ...shot().data };
      if (s === "offline") return new Response("engine offline", { status: 503 });
      if (s === "loading") return hang();
      if (req.method === "GET" && shot().fail?.includes(path)) return Response.json({ detail: "database is locked" }, { status: 500 });
      if (req.method === "GET" && data[path] !== undefined) return Response.json(data[path]);
      if (path === "/api/events") {
        // an open stream with nothing on it, until the page goes away or the server closes
        let close = () => {};
        const body = new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(new TextEncoder().encode(": connected\n\n"));
            close = () => {
              hanging.delete(close);
              try {
                c.close();
              } catch {
                /* already closed */
              }
            };
            hanging.add(close);
          },
          cancel: () => close(),
        });
        return new Response(body, { headers: { "content-type": "text/event-stream", "cache-control": "no-cache" } });
      }
      return Response.json({ detail: "Not Found" }, { status: 404 });
    }
    if (path.endsWith("/")) path += "index.html";
    if (path.includes("..")) return new Response("bad path", { status: 400 });
    try {
      const body = await Deno.readFile(`${DIST}${path}`);
      return new Response(body, { headers: { "content-type": TYPES[path.split(".").pop() ?? ""] ?? "application/octet-stream" } });
    } catch {
      return new Response("not found", { status: 404 });
    }
  });
  return { server, close: () => [...hanging].forEach((f) => f()) };
}

/** A minimal DevTools protocol client over the browser's WebSocket (flattened sessions). */
class Cdp {
  private next = 1;
  private calls = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private waiters: { method: string; sessionId?: string; resolve: () => void }[] = [];
  private constructor(private ws: WebSocket) {
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data as string);
      if (msg.id) {
        const call = this.calls.get(msg.id);
        this.calls.delete(msg.id);
        if (msg.error) call?.reject(new Error(`${msg.error.message} (${msg.error.code})`));
        else call?.resolve(msg.result);
      } else {
        this.waiters = this.waiters.filter((w) => {
          if (w.method !== msg.method || w.sessionId !== msg.sessionId) return true;
          w.resolve();
          return false;
        });
      }
    };
  }

  static connect(url: string): Promise<Cdp> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.onopen = () => resolve(new Cdp(ws));
      ws.onerror = () => reject(new Error(`can't connect to ${url}`));
    });
  }

  send<T = Record<string, unknown>>(method: string, params: Record<string, unknown> = {}, sessionId?: string): Promise<T> {
    const id = this.next++;
    this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    return new Promise((resolve, reject) => this.calls.set(id, { resolve: resolve as (v: unknown) => void, reject }));
  }

  once(method: string, sessionId?: string): Promise<void> {
    return new Promise((resolve) => this.waiters.push({ method, sessionId, resolve }));
  }

  close(): void {
    this.ws.close();
  }
}

async function launchChrome(chrome: string): Promise<{ cdp: Cdp; stop: () => Promise<void> }> {
  const profile = await Deno.makeTempDir({ prefix: "seedbox-shot-" });
  const proc = new Deno.Command(chrome, {
    args: [
      "--headless",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--hide-scrollbars",
      "--mute-audio",
      "--force-device-scale-factor=1",
      "--force-color-profile=srgb",
      "--font-render-hinting=none",
      "--disable-lcd-text",
      "--disable-gpu",
      "--disable-extensions",
      "--disable-background-timer-throttling",
      "--disable-renderer-backgrounding",
      ...(inDocker ? ["--no-sandbox"] : []),
      "about:blank",
    ],
    stdout: "null",
    stderr: "piped",
  }).spawn();
  // Chrome prints "DevTools listening on ws://…" on stderr once it is up
  const reader = proc.stderr.pipeThrough(new TextDecoderStream()).getReader();
  let log = "";
  const timeout = setTimeout(() => proc.kill(), 20_000);
  while (!/DevTools listening on (ws:\S+)/.test(log)) {
    const { value, done } = await reader.read();
    if (done) throw new Error(`Chrome exited before it was ready:\n${log}`);
    log += value;
  }
  clearTimeout(timeout);
  void (async () => {
    while (!(await reader.read()).done) {/* drain */}
  })();
  const cdp = await Cdp.connect(log.match(/DevTools listening on (ws:\S+)/)![1]);
  return {
    cdp,
    stop: async () => {
      cdp.close();
      proc.kill();
      await proc.status;
      await Deno.remove(profile, { recursive: true }).catch(() => {});
    },
  };
}

// No motion and no blinking caret, so a screenshot shows where things end up, the same every time.
const STILL_CSS = `*, *::before, *::after {
  animation-duration: 0s !important; animation-delay: 0s !important; animation-iteration-count: 1 !important;
  transition: none !important; caret-color: transparent !important;
}`;

const KEY_CODES: Record<string, [code: string, keyCode: number]> = {
  k: ["KeyK", 75],
  Enter: ["Enter", 13],
  "?": ["Slash", 191],
  Escape: ["Escape", 27],
};

async function press(cdp: Cdp, session: string, combo: string): Promise<void> {
  const parts = combo.split("+");
  const key = parts.pop()!;
  const ctrl = parts.includes("Mod") || parts.includes("Ctrl");
  const shift = key === "?" || parts.includes("Shift");
  const modifiers = (ctrl ? 2 : 0) | (shift ? 8 : 0);
  const [code, keyCode] = KEY_CODES[key] ?? [`Key${key.toUpperCase()}`, key.toUpperCase().charCodeAt(0)];
  const base = { key, code, windowsVirtualKeyCode: keyCode, modifiers };
  await cdp.send("Input.dispatchKeyEvent", { type: ctrl ? "rawKeyDown" : "keyDown", text: ctrl ? undefined : key === "Enter" ? "\r" : key, ...base }, session);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base }, session);
}

async function shoot(cdp: Cdp, url: string, shot: Shot, [width, height]: readonly [number, number]): Promise<Uint8Array> {
  const { targetId } = await cdp.send<{ targetId: string }>("Target.createTarget", { url: "about:blank", newWindow: true });
  const { sessionId } = await cdp.send<{ sessionId: string }>("Target.attachToTarget", { targetId, flatten: true });
  try {
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false }, sessionId);
    await cdp.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }, { name: "prefers-color-scheme", value: "dark" }] }, sessionId);
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
      source: `document.addEventListener("DOMContentLoaded", () => {
        const s = document.createElement("style"); s.textContent = ${JSON.stringify(STILL_CSS)}; document.head.append(s);
      });`,
    }, sessionId);
    const loaded = cdp.once("Page.loadEventFired", sessionId);
    await cdp.send("Page.navigate", { url }, sessionId);
    await loaded;
    // the pointer rests on the top bar's empty middle-right, away from anything with a hover state
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: Math.round(width * 0.72), y: 22 }, sessionId);
    await cdp.send("Runtime.evaluate", { expression: "document.fonts.ready.then(() => true)", awaitPromise: true }, sessionId);
    await new Promise((r) => setTimeout(r, shot.settle ?? 1200));
    for (const combo of shot.keys ?? []) {
      await press(cdp, sessionId, combo);
      await new Promise((r) => setTimeout(r, 400));
    }
    const { data } = await cdp.send<{ data: string }>("Page.captureScreenshot", { format: "png" }, sessionId);
    return Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
  } finally {
    await cdp.send("Target.closeTarget", { targetId }).catch(() => {});
  }
}

type Outcome = "same" | "changed" | "new" | "updated";

/** Compares a screenshot with its baseline; writes the diff, or the baseline when updating. */
async function compare(file: string, png: Uint8Array): Promise<{ outcome: Outcome; detail: string }> {
  const baselinePath = `${BASELINES}/${file}`;
  await Deno.writeFile(`${OUT}/actual/${file}`, png);
  if (!(await exists(baselinePath))) {
    if (update) {
      await Deno.writeFile(baselinePath, png);
      return { outcome: "updated", detail: "new baseline" };
    }
    return { outcome: "new", detail: "no baseline" };
  }
  const baseline = await Deno.readFile(baselinePath);
  let detail: string;
  try {
    const d = diffImages(await decodePng(baseline), await decodePng(png), PIXEL_THRESHOLD);
    detail = `${d.changed} px (${((d.changed / d.total) * 100).toFixed(3)} %)`;
    if (d.changed <= MAX_CHANGED) return { outcome: "same", detail };
    if (!update) {
      await Deno.writeFile(`${OUT}/diff/${file}`, await encodePng(d.image));
      await Deno.writeFile(`${OUT}/expected/${file}`, baseline);
    }
  } catch (e) {
    detail = (e as Error).message;
  }
  if (update) {
    await Deno.writeFile(baselinePath, png);
    return { outcome: "updated", detail };
  }
  return { outcome: "changed", detail };
}

async function main(): Promise<number> {
  if (!flags.includes("--no-build") && !inDocker) await run(Deno.execPath(), ["task", "build"]);
  if (!inDocker && !flags.includes("--no-docker")) return await runInDocker();
  if (!inDocker) console.warn("--no-docker: this machine's Chrome and fonts won't match the baselines\n");

  const shots = SHOTS.filter((s) => !only || only.has(s.name));
  if (only && shots.length !== only.size) throw new Error(`unknown --only name; the shots are: ${SHOTS.map((s) => s.name).join(", ")}`);
  await Deno.remove(OUT, { recursive: true }).catch(() => {});
  for (const d of ["actual", "diff", "expected"]) await Deno.mkdir(`${OUT}/${d}`, { recursive: true });
  await Deno.mkdir(BASELINES, { recursive: true });

  let current = shots[0];
  const { server, close } = serve(() => current);
  const { cdp, stop } = await launchChrome(await findChrome());
  const results: { file: string; outcome: Outcome; detail: string }[] = [];
  try {
    const base = `http://127.0.0.1:${server.addr.port}/`;
    for (const shot of shots) {
      for (const size of SIZES) {
        current = shot;
        const file = `${shot.name}-${size[0]}x${size[1]}.png`;
        const png = await shoot(cdp, base + shot.hash, shot, size);
        close(); // end the page's hanging requests and event stream
        const r = await compare(file, png);
        results.push({ file, ...r });
        console.log(`${r.outcome.padEnd(8)} ${file.padEnd(36)} ${r.detail}`);
      }
    }
  } finally {
    await stop();
    close();
    await server.shutdown();
  }

  // baselines of shots that no longer exist
  const stale: string[] = [];
  if (!only) {
    const files = new Set(results.map((r) => r.file));
    for await (const e of Deno.readDir(BASELINES)) {
      if (!e.name.endsWith(".png") || files.has(e.name)) continue;
      if (update) await Deno.remove(`${BASELINES}/${e.name}`);
      stale.push(e.name);
    }
    for (const f of stale) console.log(`${update ? "removed " : "stale   "} ${f}`);
  }

  const failed = results.filter((r) => r.outcome === "changed" || r.outcome === "new");
  if (failed.length || (stale.length && !update)) {
    console.log(`\n${failed.length} of ${results.length} screenshots changed or are new${stale.length ? `, ${stale.length} baselines are stale` : ""}.`);
    console.log(`See screenshots/{actual,expected,diff}/. If the change is intended: UPDATE_SCREENSHOTS=1 deno task screenshot`);
    return 1;
  }
  console.log(`\n${results.length} screenshots ${update ? "written to visual-baselines/" : "match their baselines"}.`);
  return 0;
}

Deno.exit(await main());
