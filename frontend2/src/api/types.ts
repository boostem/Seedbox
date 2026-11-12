// The API contract between the Seedbox 2 server (seedbox2/server/) and frontend2. snake_case on the wire.
// The analysis and transition shapes below are Seedbox 1's (musicreco), kept for the waveform code until the
// Seedbox 2 analysis (#43–#49) and mix plan (#51) tickets replace them.

import type { MixPlan } from "./mixplan.generated";

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

// ---- Updates and "Report a problem" (#89): /api/system/updates, /api/system/ytdlp/*, /api/system/report ----

export type AppUpdateState = "idle" | "checking" | "up_to_date" | "available" | "downloading" | "ready" | "error";

export interface AppRelease {
  version: string;
  tag: string;
  name: string;
  url: string; // the release page
  notes: string;
  published_at: string | null;
  asset: { name: string; size: number; url: string } | null; // this computer's installer
}

export interface AppUpdateStatus {
  current: string;
  enabled: boolean; // check at startup (settings.json `updates.check`)
  installable: boolean; // false from source and on Linux: the release is only linked to
  reason: string | null; // why not installable
  state: AppUpdateState;
  latest: AppRelease | null;
  progress: { done: number; total: number } | null;
  error: string | null;
  checked_at: number | null; // unix seconds
  install_on_exit: boolean;
  releases_url: string;
}

export interface YtdlpStatus {
  running: string;
  bundled: string | null;
  installed: string | null; // a downloaded copy (in use, or used after a restart)
  latest: string | null; // PyPI's newest, once the background check answered
  update_available: boolean;
  checking: boolean;
  updating: boolean;
  error: string | null;
  restart: boolean; // a downloaded copy waits for the next launch
  can_roll_back: boolean;
}

export interface UpdatesStatus {
  app: AppUpdateStatus;
  ytdlp: YtdlpStatus;
}

export interface ProblemReport {
  path: string;
  size: number;
  files: string[];
  issue_url: string; // a new GitHub issue, versions filled in
  self_test_ok: boolean | null;
}

// ---- First run (#80): GET/POST /api/onboarding, GET /api/tunebat/search, POST /api/tunebat/download ----

export interface Onboarding {
  first_run: boolean;
}

export interface TunebatTrack {
  id: string;
  name: string;
  artists: string[];
  album: string;
  duration: number; // seconds
  bpm: number;
  camelot: string;
  cover_url: string | null;
}

/** A `tunebat.download` event, and the answer to POST /api/tunebat/download. */
export interface TunebatDownload {
  id: string;
  state: "queued" | "finding" | "downloading" | "done" | "failed";
  error: string | null;
}

// ---- A crate from tunebat: GET /api/tunebat/crates/{seed}, POST .../{seed}/download, GET /api/tunebat/crates/downloads ----

export interface TunebatCrateTrack extends TunebatTrack {
  key: string;
  energy: number;
}

export interface TunebatRec {
  track: TunebatCrateTrack;
  /** 0-100, musicreco's mixing score with the seed: what the list is sorted by. */
  score: number;
  move: Move;
  chips: string[];
  /** Seedbox 1's pre-tick rule: a compatible key and the tempo within the tolerance (half and double time count). */
  related: boolean;
  reason: string;
  in_library: boolean;
}

export interface TunebatCrate {
  seed: TunebatCrateTrack & { in_library: boolean };
  /** The crate's folder in the music folder, and its name once imported. */
  folder: string;
  tolerance: number;
  recs: TunebatRec[];
}

export interface TunebatCrateItem {
  id: string;
  name: string;
  artists: string[];
  /** What it is doing now: "queued", "checking library", "searching", "downloading", … then its status. */
  text: string;
  status: "downloaded" | "copied" | "exists" | "skipped" | "failed" | null;
  reason: string | null;
  /** the library track it became, once imported (its analysis can be followed from there) */
  hash?: string | null;
}

/** A `tunebat.crate` event, and the answer to POST /api/tunebat/crates/{seed}/download. */
export interface TunebatCrateJob {
  id: string;
  seed_id: string;
  seed: string;
  folder: string;
  state: "queued" | "running" | "finishing" | "done" | "cancelled" | "failed";
  error: string | null;
  path?: string;
  items: TunebatCrateItem[];
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
  low_power: boolean; // analysis uses the small models (Beat This! small0 instead of final0)
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

// ---- Low-power mode: GET /api/power, the `power` event (#85) ----

export type PowerMode = "auto" | "on" | "off";

export interface PowerStatus {
  mode: PowerMode;
  low_power: boolean;
  reason: "battery" | "thermal" | "manual" | null;
  on_battery: boolean | null; // null: this machine has no battery
  battery_percent: number | null;
  thermal: "nominal" | "fair" | "serious" | "critical" | "unknown";
  limits: {
    stems_paused: boolean; // background stems wait; what the user opened still runs
    analysis_paused: boolean; // the library's analysis waits too (critical heat)
    light_workers: number;
    light_workers_full: number;
    fps: number | null; // the cap on waveform drawing
  };
}

// ---- The master chain: GET /api/mixes/master (#58) ----

export type MasterTarget = "streaming" | "club";

export interface MasterStatus {
  target: MasterTarget;
  target_lufs: number;
  ceiling_dbtp: number;
  makeup_db: number; // open-loop: what the live engine plays with until a render has measured one
  targets: Record<MasterTarget, number>;
}

// ---- Stems: GET /api/analysis/stems, /api/sets/{id}/stems (#44) ----

export interface StemsStatus {
  limit_gb: number;
  format: "flac" | "opus";
  low_power: boolean;
  enabled: boolean; // false: stems are off, none are separated and the mix uses EQ
  usage: { tracks: number; bytes: number };
}

export interface SetStemsStatus {
  set_id: number;
  /** every track of the set has its stems */
  ready: boolean;
  ready_tracks: number;
  total: number;
  /** 0-1 over the whole set */
  progress: number;
  tracks: { hash: string; stems: boolean | "queued" | "running" | "failed"; progress: number | null }[];
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
  slot?: number | null; // hot cue slot 0-7 (A-H), coloured by `cue-1`…`cue-8`; none for a cue the analysis placed
}

/** One piece of a piecewise beatgrid (seedbox2/analysis/grid.py): from beat `start_beat` at `start_s`, at `bpm`. */
export interface GridSegment {
  start_beat: number;
  start_s: number;
  bpm: number;
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
  grid?: GridSegment[] | null; // the piecewise beatgrid when there is one; bar lines follow it instead of bar_s
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

// ---- The library view (#73: seedbox2/library/view.py) ----

export type AnalysisState = "new" | "queued" | "analysing" | "failed" | "done";

/** One row of GET /api/library/view: what the library table shows, sorts and filters on. */
export interface KeyNames {
  key: string; // "A minor"
  camelot: string;
  open_key: string;
}

export interface KeyAnalysis {
  key: string | null;
  camelot: string | null;
  open_key: string | null;
  confidence: number;
  best_guess: KeyNames | null;
  source: "stems" | "mix";
  beat_synchronous: boolean;
  tuning_cents: number;
  changes: (KeyNames & { start_s: number; end_s: number; confidence: number })[];
}

export interface KeyStatus {
  analysis: KeyAnalysis | null;
  effective: { key: string | null; camelot: string | null; open_key: string | null; from: "user" | "analysis" | "unknown" };
  hints: { key: string; from: string }[];
}

export interface LibraryRow {
  hash: string;
  duration: number;
  title: string;
  artist: string;
  album: string;
  source: string;
  added_at: string;
  bpm: number | null;
  /** "analysis" (ours) or "tags" (a hint from the file, Rekordbox, iTunes or Seedbox 1) */
  bpm_from: "analysis" | "tags" | null;
  key: string | null; // Camelot code
  /** "user" (their own key), "analysis", "unknown" (analysed, not confident: shown as "?") or "tags" (a hint, before analysis) */
  key_from: "user" | "analysis" | "unknown" | "tags" | null;
  /** A tag's key that disagrees with ours, shown next to it */
  key_hint: string | null;
  key_hint_from: "rekordbox" | "tunebat" | "file" | "itunes" | null;
  energy: number | null; // 0-1
  energy_from: "analysis" | "tags" | null;
  genre: string;
  /** genre, comment, label…: what the vibe search looks through besides title, artist and album */
  text: string;
  artwork: boolean;
  format: string;
  status: {
    /** "done": ready and nothing pending */
    state: AnalysisState;
    /** every analysis mixing and sets read has finished: beats, key, energy, sections, vocals, waveform, and stems unless they are off */
    ready: boolean;
    /** what it lacks, in that order (a failed one counts) */
    missing: string[];
    /** the user said to use this failed track anyway (PUT /library/tracks/{hash}/use-anyway) */
    use_anyway: boolean;
    /** what sets, mixes and exports take: ready, or failed and use_anyway; the API answers 409 for the others */
    usable: boolean;
    waveform: boolean;
    /** true once separated; else the stems job's state, or false if there is none */
    stems: boolean | "queued" | "running" | "failed";
    stems_progress: number | null;
    analyses: string[];
    jobs: Record<string, "queued" | "running" | "failed">;
  };
}

export interface CrateSummary {
  id: number;
  name: string;
  folder: string | null;
  source: string;
  track_count: number;
  updated_at: string;
}

export interface SetSummary {
  id: number;
  name: string;
  crate_id: number | null;
  source: string;
  track_count: number;
  updated_at: string;
}

/** A job row (GET /api/jobs, the job.* events). */
export interface JobRow {
  id: string;
  kind: string;
  state: "queued" | "running" | "done" | "failed" | "cancelled";
  priority: number;
  payload: { hash?: string } & Record<string, unknown>;
  progress: number;
  error: string | null;
  result?: unknown;
  created_at: string;
  updated_at: string;
}

/** What a finished `set_export` job says (#70). */
export interface SetExportResult {
  path: string;
  tracklist: string;
  cue: string;
  format: string;
  duration_s: number;
  target: string;
  measured: { integrated_lufs: number; lra_lu: number | null; true_peak_dbtp: number };
}

export interface SetExportStatus {
  formats: { id: string; label: string }[];
  folder: string;
  targets: Record<string, number>;
  job: JobRow | null;
}

export interface JobsResponse {
  status: { pools: Record<string, { workers: number; size: number; busy: number; queued: number }>; running: number; queued: number; waiting_for_ram: boolean };
  types: string[];
  /** each job type's pool (light or heavy) */
  pools?: Record<string, string>;
  items: JobRow[];
}

/** The blind listening test (#86, seedbox2/eval/listening.py). */
export interface ListenProgress {
  done: number;
  total: number;
}

export interface ListenStatus {
  folder: string;
  pairs: number;
  renders: number;
  comparisons: number;
  ratings: number;
  progress: ListenProgress;
}

export interface ListenTrial {
  id: string;
  pair: string;
  progress: ListenProgress;
}

export type ListenSlot = "a" | "b";
export type ListenScore = "smoothness" | "energy" | "dj";
export type ListenScores = Partial<Record<ListenSlot, Partial<Record<ListenScore, number>>>>;

export interface ListenRating {
  choice: ListenSlot;
  scores: ListenScores;
  comment: string;
  rater: string;
}

export interface ListenRate {
  trials: number;
  wins: number;
  win_rate: number | null;
  ci_low: number;
  ci_high: number;
}

export interface ListenRow extends ListenRate {
  name: string;
  scores: Partial<Record<ListenScore, number>>;
}

export interface ListenMatchup extends ListenRate {
  name: string;
  opponent: string;
}

export interface ListenSummary {
  ratings: number;
  raters: number;
  versions: ListenRow[];
  styles: ListenRow[];
  matchups: ListenMatchup[];
  slot_a: ListenRate;
}

export interface ListenImport {
  added: number;
  duplicates: number;
  ratings: number;
}

// ---- Ordering a set: /api/sets/arcs, /api/sets/{id}/order (#60) ----

export interface ArcTemplate {
  name: string;
  label: string;
  description: string;
  /** [time 0-1, level 0-1], the set's quietest track at level 0 and its most intense at 1. */
  points: [number, number][];
}

export interface SetPin {
  position: number;
  hash: string;
}

export interface SetOrderSettings {
  /** A template's name, a drawn curve, or null for the default. */
  arc: string | [number, number][] | null;
  tempo_start: number | null;
  tempo_end: number | null;
  pins: SetPin[];
  /** The assistant's reasoning, when it made this set (#63). */
  note?: string | null;
}

export interface SetOrderTrack {
  position: number;
  hash: string;
  title: string;
  artist: string;
  bpm: number | null;
  key: string | null;
  /** 0-1. */
  energy: number | null;
  /** 1-10. */
  rating: number | null;
  /** Semitones the track is played away from its key. */
  shift: number;
  pinned: boolean;
  target_energy: number;
  target_bpm: number | null;
}

export type SetTransitionFlag = "key_clash" | "key_fix" | "tempo_wide" | "tempo_over_limit" | "genre_change" | "vocal_run";

export interface SetTransition {
  from: string;
  to: string;
  /** "energy +1, 8A → 9A, same vibe" */
  text: string;
  flags: SetTransitionFlag[];
  cost: number;
  /** 0-100, from what the two tracks cost each other. */
  score: number;
  /** Under the threshold: worth finding a bridge. */
  gap: boolean;
  /** "Why this transition?" in plain sentences, from the same numbers (and the saved mix plan, if there is one). */
  why?: string[];
}

/** What would fit between two neighbours (#64). */
export interface BridgeWant {
  bpm: number | null;
  keys: string[];
  energy: [number, number] | null;
  /** "a 124 BPM 8A/9A track with a long intro and a similar vibe" */
  text: string;
}

export interface BridgeGap {
  from: string;
  to: string;
  text: string;
  score: number;
  cost: number;
}

export interface LibraryBridge {
  hash: string;
  title: string;
  artist: string;
  bpm: number;
  key: string;
  energy: number | null;
  rating: number | null;
  shift: number;
  intro_s: number | null;
  cost: number;
  /** The scores of the two transitions it would make. */
  score_in: number;
  score_out: number;
  text: string;
}

export interface LibraryBridges {
  gap: BridgeGap;
  want: BridgeWant;
  results: LibraryBridge[];
  /** Analysed tracks outside the set. */
  candidates: number;
  /** Tracks outside the set that aren't ready to mix (still being analysed, or failed). */
  unanalysed: number;
}

export interface OnlineBridge {
  id: string;
  name: string;
  artists: string[];
  bpm: number;
  camelot: string;
  shift: number;
  cover_url: string | null;
  /** Whose recommendation it is. */
  from: ("from" | "to")[];
  cost: number;
  score_in: number;
  score_out: number;
  text: string;
}

export interface OnlineBridges {
  gap: BridgeGap;
  /** A neighbour tunebat doesn't know has no id. */
  seeds: { hash: string; id: string | null }[];
  results: OnlineBridge[];
}

export interface SetSourceRef {
  kind: "crate" | "set" | "library";
  id: number | null;
}

/** One track of a built long set. */
export type LongSetTrack = SetOrderTrack & {
  /** Index into `sources`; a track put in by hand has none. */
  source?: number;
};

export interface LongSetSource extends SetSourceRef {
  name: string;
  tracks: number;
  picked: number;
}

/** A long set across crates, not saved yet. After a track is put in, `POST /api/sets/build/explain` has no `sources`. */
export interface LongSetResult extends Omit<SetOrderResult, "order"> {
  order: LongSetTrack[];
  sources?: LongSetSource[];
  /** Tracks of the sources that aren't in it, for the length asked. */
  left_out?: number;
  /** Tracks playing outside their source's share of the set. */
  moved?: number;
}

export interface LinkTrack {
  hash: string;
  title: string;
  artist: string;
  bpm: number | null;
  key: string | null;
  energy: number | null;
  rating: number | null;
}

export interface CrateLink {
  from_source: number;
  to_source: number;
  from: LinkTrack;
  to: LinkTrack;
  cost: number;
  text: string;
  flags: SetTransitionFlag[];
  fit: "good" | "fair" | "poor" | "unknown";
}

export interface SetOrderResult {
  order: SetOrderTrack[];
  transitions: SetTransition[];
  cost: number;
  /** What the set's saved order costs under the same settings. */
  cost_before: number;
  arc: ArcTemplate;
  tempo: { start: number; end: number; auto: boolean } | null;
  /** How many tracks don't have each thing yet. */
  unknown: { bpm: number; key: number; energy: number; vibe: number; vocals: number; genre: number };
  settings: SetOrderSettings;
}

// ---- Now playing (GET /api/sets/{id}/session, #77) ----

export interface SessionTrack {
  hash: string;
  title: string | null;
  artist: string | null;
  duration: number;
  /** The beats analysis, or null until it has run. */
  grid: { bpm: number; first_downbeat_s: number; beats_per_bar: number; segments?: GridSegment[] | null } | null;
  /** Phrase starts from the sections analysis: where each begins and how many bars it runs (8, 16 or 32). */
  phrases?: { start_s: number; level: 8 | 16 | 32 }[];
  /** Sections, or rough regions of the energy curve (`main`) until the track has them. */
  sections: { kind: string; start_s: number; end_s: number }[];
  key: { key: string | null; camelot: string | null };
  integrated_lufs: number | null;
  /** The gain that puts its body at the bus's level, as the export plays it. Null until it has been analysed. */
  track_gain_db?: number | null;
}

export interface SetSession {
  id: number;
  name: string;
  tracks: SessionTrack[];
  /** One per neighbouring pair of tracks; null where the pair has no plan. */
  plans: (MixPlan | null)[];
}

// ---- The whole-set timeline (seedbox2/sets/timeline.py) ----

export interface TimelineTrack {
  hash: string;
  duration_s: number;
  /** Its sections, or the energy curve's intro, main and outro until it has them. */
  sections: { kind: string; start_s: number; end_s: number }[];
  /** [file time, level 0-1]. */
  curve: [number, number][];
  /** A bar on the track's grid; null before its beats are analysed. */
  bar_s: number | null;
  /** What brings it to the deck level when it plays; null before it's measured. */
  integrated_lufs?: number | null;
  track_gain_db?: number | null;
}

/** `pending`: its plan is being made. `waiting`: a track's beats have to come first. */
export type TimelineTransitionState = "ready" | "pending" | "waiting" | "failed";

export interface TimelineTransition {
  from: string;
  to: string;
  state: TimelineTransitionState;
  error?: string;
  technique?: string;
  bars?: number;
  edited?: boolean;
  user_edited?: boolean;
  /** How long both decks sound on the master clock, and where that is in each file. */
  seconds?: number;
  a_in_s?: number;
  a_out_s?: number;
  b_in_s?: number;
  b_out_s?: number;
}

export interface SetTimelineData {
  tracks: TimelineTrack[];
  transitions: TimelineTransition[];
}

// --- the set assistant (#63) ---

export interface AssistantStatus {
  enabled: boolean;
  model: string;
  models: { id: string; label: string }[];
  has_key: boolean;
  /** Is there an OS keychain to keep the key in? */
  keychain: boolean;
}

/** Which tracks a run may use. Neither: the whole library. */
export interface AssistantScope {
  set_id?: number;
  crate_ids: number[];
}

export interface AssistantDraft {
  name: string;
  reasoning: string;
  result: SetOrderResult;
  steps: string[];
  model: string;
  usage: { input_tokens: number; output_tokens: number };
}

export interface AssistantDryRun {
  url: string;
  headers: Record<string, string>;
  body: unknown & object;
  example_search_result: unknown;
}
