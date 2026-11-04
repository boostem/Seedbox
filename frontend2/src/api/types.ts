// The API contract between the Seedbox 2 server (seedbox2/server/) and frontend2. snake_case on the wire.
// The analysis and transition shapes below are Seedbox 1's (musicreco), kept for the waveform code until the
// Seedbox 2 analysis (#43–#49) and mix plan (#51) tickets replace them.

// ---- System: GET /api/health, GET /api/system, POST /api/system/reveal ----

export interface Health {
  ok: boolean;
}

export interface SystemInfo {
  app: string; // "Seedbox 2"
  version: string;
  python: string;
  platform: string; // sys.platform: "darwin", "win32", "linux"
  machine: string;
  frozen: boolean;
  ffmpeg: string | null;
  deno: string | null;
  frontend: string | null; // the dist/ folder the server serves
  folders: SystemFolders; // this OS's folders (seedbox2/paths.py), or the $SEEDBOX_* overrides
  reveal_label: string; // "Show in Finder" / "Show in Explorer" / "Show in file manager"
}

export interface SystemFolders {
  settings: string;
  cache: string;
  logs: string;
  music: string; // the music library shared with Seedbox 1
}

// ---- Models: GET /api/models, POST /api/models/{name}/download|cancel, DELETE /api/models/{name} (#42) ----

export type ModelState = "installed" | "missing" | "downloading" | "failed";

export interface ModelInfo {
  name: string; // "beat_this"
  version: string;
  title: string;
  file: string;
  url: string;
  sha256: string;
  size: number; // bytes
  licence: string;
  attribution: string;
  homepage: string;
  used_for: string;
  fallback: string; // what runs while the model isn't there
  inputs: string;
  state: ModelState;
  installed: boolean;
  path: string | null;
  done: number; // bytes downloaded (a partial download resumes from here)
  error: string | null;
}

export interface ModelSettings {
  accelerator: boolean; // CoreML on macOS, DirectML on Windows (the CPU is always the fallback)
  auto_download: boolean; // download missing models when the app starts
}

export interface ModelsStatus {
  dir: string;
  models: ModelInfo[];
  missing: { name: string; title: string; used_for: string; fallback: string }[];
  offline: boolean; // some analysis runs on the Seedbox 1 heuristics
  installed_bytes: number;
  total_bytes: number;
  settings: ModelSettings;
}

// ---- Library: GET /api/library, /api/library/tracks, /api/library/folders, /api/library/import (#38, #40) ----

export interface LibrarySummary {
  path: string;
  tracks: number;
  track_paths: number;
  crates: number;
  sets: number;
}

export interface LibraryTrack {
  hash: string;
  duration: number;
  title: string;
  artist: string;
  album: string;
  /** Hints, not truth: `file` (the file's own tags), `rekordbox`, `itunes`, `tunebat`, `artwork` (a path). */
  tags: Record<string, unknown> & { file?: Record<string, unknown> };
  source: string; // local, seedbox1, rekordbox, itunes...
  added_at: string;
}

export interface WatchFolders {
  folders: { path: string; exists: boolean }[];
  scanning: string | null;
}

/** The latest import, from GET /api/library/import and the `library.import` events. */
export interface ImportStatus {
  state: "idle" | "queued" | "running" | "done" | "failed";
  kind?: "files" | "xml" | "watch" | string;
  label?: string;
  folder?: string | null;
  done?: number;
  total?: number | null;
  file?: string;
  files?: number;
  added?: number;
  updated?: number;
  removed?: number;
  crates?: number;
  tracks?: number;
  missing?: number;
  error_count?: number;
  errors?: string[];
  error?: string;
}

// ---- Server-sent events: GET /api/events (text/event-stream, one JSON object per `data:` line) ----

export interface ServerEvent {
  type: string;
  data: unknown;
}

export type Move = "same" | "+1" | "-1" | "relative" | "+2 boost" | "+7 boost" | "diagonal" | "clash" | "?";
// ---- Audio analysis (musicreco/structure.py) ----

export type SectionKind = "intro" | "build" | "drop" | "breakdown" | "body" | "outro";

export interface Section {
  kind: SectionKind;
  start_bar: number; // 0-based bar index
  end_bar: number; // exclusive
  start_s: number;
  end_s: number;
}

export interface Edge {
  // intro (start of track) or outro (end of track)
  bars: number; // length in bars (phrase-aligned when possible: 8/16/32)
  start_s: number;
  end_s: number;
  drums_only: boolean;
  has_vocals: boolean;
}

export interface CuePoint {
  bar: number;
  time_s: number;
  label: string; // "first downbeat", "mix in (16-bar intro)", "drop", "mix out (32-bar outro)", ...
}

export interface Waveform {
  points_per_second: number;
  low: number[]; // 0-1 per point, three bands like rekordbox's 3-band waveform
  mid: number[];
  high: number[];
}

export interface TrackAnalysis {
  version: number;
  track_id: string;
  duration_s: number;
  bpm: number; // grid tempo actually used
  bpm_source: "tunebat" | "detected";
  first_downbeat_s: number;
  bar_s: number;
  bar_count: number;
  phrase_offset: number; // 0-7: bar index where the 8-bar phrase grid starts (phrases begin at bars phrase_offset + 8k)
  phrases?: { bar: number; level: 8 | 16 | 32 }[]; // every phrase start and the phrase it opens (8-bar grid re-anchored at sections; analysis v5+)
  energy_per_bar: number[]; // 0-1 overall
  low_per_bar: number[]; // kick/bass presence 0-1
  vocal_per_bar: number[]; // vocal likelihood 0-1 (heuristic)
  sections: Section[];
  intro: Edge;
  outro: Edge;
  cues: CuePoint[];
  waveform: Waveform;
}

// ---- Mix suggestions (musicreco/transitions.py) ----

export type Technique = "long blend" | "phrase blend" | "bass swap" | "drop swap" | "quick cut" | "echo out";

export interface Transition {
  from_id: string;
  to_id: string;
  score: number; // 0-100
  key_move: Move;
  pitch_pct: number; // tempo change needed on the incoming track, %
  out_start_s: number | null; // where to start bringing B in, in A's timeline
  in_start_s: number | null; // B's cue point to start from
  overlap_bars: number | null;
  swap_bar: number | null; // 1-based bar within the overlap where B's low end replaces A's (any blend that swaps bass, incl. blend → echo out); null for cuts / no bass
  technique: Technique | null;
  a_loop_bars?: number; // overlap bars A loops a phrase of its outro once its music runs out (0 = none)
  b_loop_bars?: number; // overlap bars B loops a phrase of its short intro from its cue (0 = none)
  // set transitions only (musicreco/setplan.py): the planner technique the set plays here, chosen across the whole set
  // so it doesn't repeat itself (null until both tracks are analysed), and its blend length (0 for a cut)
  plan_technique?: string | null;
  plan_bars?: number | null;
  notes: string[]; // human explanations
  warnings: string[]; // e.g. "vocals overlap for 8 bars"
  ready: boolean; // false until both analyses exist (then only key/BPM fields are meaningful)
}

// ---- Mix metrics (seedbox2/eval/mixmetrics.py, #54) ----

export type MixCheckStatus = "pass" | "warn" | "fail" | "na";

/** One objective check on a rendered transition (mixmetrics.checks): the transition editor (#79) lists them. */
export interface MixCheck {
  id: "beat" | "bass" | "vocals" | "loudness" | "key" | "masking";
  label: string; // "One bassline at a time"
  status: MixCheckStatus;
  value: string; // "1.8 s"
  detail: string; // what was measured, for the tooltip
}
