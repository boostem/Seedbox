// Shared API contract between the Seedbox backend (seedbox/server.py) and the frontend.
// Source of truth: APP_SPEC.md. Keep both sides in sync; snake_case on the wire.

export interface Track {
  id: string; // Spotify track ID
  name: string;
  artists: string[];
  display_name: string; // "Artists - Title"
  album: string;
  release_date: string;
  duration_s: number;
  popularity: number;
  key: string; // "G♯ minor"
  camelot: string; // "1A"
  bpm: number;
  energy: number; // 0-1
  danceability: number; // 0-1
  cover_url: string | null; // remote Spotify art, or "/api/covers/<folder>/<track_id>" (art embedded in the MP3) when unknown
  spotify_url: string;
}

export type Move = "same" | "+1" | "-1" | "relative" | "+2 boost" | "+7 boost" | "diagonal" | "clash" | "?";

export interface Score {
  value: number; // 0-100
  passed: boolean; // the user's pre-tick rule (Camelot classic-adjacent AND BPM ±10%, half/double ok)
  move: Move;
  chips: string[];
}

export interface Rec {
  track: Track;
  score: Score;
  rule_reason: string;
  ticked_default: boolean; // true when settings.pretick_all, else score.passed
  existing_path: string | null; // already in this crate's folder
}

export interface SearchPage {
  page: number;
  total_count: number;
  has_next: boolean;
  items: Track[];
}

export interface Crate {
  folder: string; // crate id = seed folder name ("Artists - Title")
  path: string; // absolute folder path
  seed: Track;
  seed_existing_path: string | null;
  recs: Rec[]; // sorted by score desc
}

export interface PreviewStatus {
  track_id: string;
  state: "idle" | "queued" | "loading" | "ready" | "no_match" | "error";
  audio_url: string | null; // "/api/previews/<id>/audio" when ready
  peaks: number[] | null; // clip waveform, ~200 values 0-1, when ready
  youtube_title: string | null; // set for ready / error
  error: string | null; // for no_match: the reason, e.g. "no result within ±15s (closest 3:41 on ytmusic)"
}

export type FinalStatus = "downloaded" | "copied" | "exists" | "skipped" | "failed";

export interface JobItem {
  track: Track;
  text: string; // live step: "queued" | "searching" | "downloading" | "genre lookup" | ... | final status
  status: FinalStatus | null; // set when done
  reason: string | null;
  genre: string | null;
  path: string | null;
  youtube_url: string | null;
}

export interface Job {
  job_id: string;
  folder: string;
  state: "running" | "finalizing" | "done" | "cancelled";
  items: JobItem[];
  started_at: string;
  finished_at: string | null;
}

export interface Settings {
  output_root: string; // default ~/Music/MusicReco
  workers: number; // parallel downloads, default 2
  tolerance_s: number; // YouTube duration tolerance, default 15
  bpm_tolerance: number; // pre-tick rule, default 0.10
  preview_concurrency: number; // default 3
  preview_cache_mb: number; // default 500
  pretick_all: boolean; // default true: every "r" rec starts ticked; false = only key/BPM rule matches
}

export interface SystemStatus {
  version: string;
  ytdlp_version: string;
  ytdlp_latest: string | null;
  ffmpeg: string | null; // path or null if missing
  deno: string | null;
  output_root: string;
}

// ---- Crate library / set builder ----

export interface CrateSummary {
  folder: string;
  path: string;
  seed: Track | null;
  track_count: number;
  updated_at: string;
}

export interface SetTrack {
  track: Track;
  path: string; // absolute mp3 path
  audio_url: string; // "/api/files/<folder>/<filename>"
  order: number; // 1-based position (TRCK)
  energy_rating: number | null; // 1-10
  genre: string | null;
  analysis_state: "missing" | "queued" | "running" | "ready" | "error";
}

export interface CrateSet {
  folder: string;
  path: string;
  seed_id: string | null;
  tracks: SetTrack[]; // in set order
  transitions: Transition[]; // tracks[i] -> tracks[i+1]; length tracks.length - 1 (entries may be partial until analysis is ready)
}

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
  notes: string[]; // human explanations
  warnings: string[]; // e.g. "vocals overlap for 8 bars"
  ready: boolean; // false until both analyses exist (then only key/BPM fields are meaningful)
}

export interface Partner {
  track: Track;
  transition: Transition; // this track's outro -> partner's intro (or reverse when direction = "in")
  direction: "out" | "in";
}

// ---- Server-sent events: GET /api/events (text/event-stream, one JSON object per `data:` line) ----

export type ServerEvent =
  | { type: "preview"; data: PreviewStatus }
  | { type: "job"; data: { job_id: string; track_id: string; text: string } }
  | { type: "job_state"; data: { job_id: string; state: Job["state"] } } // running -> finalizing etc.
  | { type: "job_done"; data: Job }
  | { type: "analysis"; data: { folder: string; track_id: string; state: SetTrack["analysis_state"] } }
  | { type: "set_changed"; data: { folder: string } }
  | { type: "render"; data: { render_id: string; folder: string; from_id: string; to_id: string; state: "running" | "done" | "error"; progress: number; error?: string } }
  | { type: "toast"; data: { level: "info" | "warning" | "error"; message: string } };

// ---- Small responses ----
export interface OkResponse { ok: boolean }
export interface UpdateResult { ok: boolean; version: string; message: string }
export interface PickFolderResult { path: string | null } // null = cancelled; 501 when no native window (browser/dev)

// ---- Mix plans & rendered transitions (musicreco/mixplan.py, musicreco/mixrender.py) — APP_SPEC.md "v4" ----
// Everything lives on one MASTER timeline measured in beats. Master beat 0 = the moment B comes in (A's out_start_s,
// on A's phrase grid). Each deck maps master beats to its own file time via its beat grid (+ loops), and is
// time-stretched with key lock so its beats land on the master clock, whose tempo follows `tempo`.

export type LaneId = "volume" | "eq_low" | "eq_mid" | "eq_high" | "filter" | "echo_send" | "echo_feedback" | "reverb_send";

export type Curve = "linear" | "hold" | "ease"; // shape from this keyframe to the next ("hold" = step at the next keyframe)

export interface Keyframe {
  beat: number; // master beat (fractions allowed)
  value: number;
  curve: Curve;
}

// Lane value ranges (renderer clamps):
//   volume        0..1   linear fader gain (1 = unity)
//   eq_low/mid/high  -30..+6 dB, 0 = flat, -30 = kill (DJ isolator, crossovers ~200 Hz / ~2.5 kHz)
//   filter        -1..1  bipolar DJ filter: <0 low-pass closing (−1 ≈ 80 Hz), >0 high-pass opening (+1 ≈ 10 kHz), 0 = bypass
//   echo_send     0..1   pre-fader send into the deck's beat-synced echo (its tail keeps ringing after the fader is down)
//   echo_feedback 0..0.95
//   reverb_send   0..1   pre-fader send into a wash reverb
export const LANE_RANGES: Record<LaneId, [number, number]> = {
  volume: [0, 1], eq_low: [-30, 6], eq_mid: [-30, 6], eq_high: [-30, 6], filter: [-1, 1],
  echo_send: [0, 1], echo_feedback: [0, 0.95], reverb_send: [0, 1],
};

export interface DeckLoop {
  start_beat: number; // master beat where the loop engages
  end_beat: number; // master beat where it releases
  source_beat: number; // master beat (in the deck's un-looped timeline) where the looped audio starts
  length_beats: number; // 1, 2, 4, 8, 16 ...
}

export interface DeckPlan {
  track_id: string;
  beat0_s: number; // this deck's file time (s) at master beat 0
  native_bpm: number; // this deck's grid tempo (analysis bpm)
  play_from_beat: number; // master beat where the deck starts sounding
  play_to_beat: number; // master beat where it stops (echo/reverb tails may ring past it)
  echo_beats: number; // echo time in beats (0.75 = dotted 8th, 1, 0.5 …)
  loops: DeckLoop[];
  lanes: Record<LaneId, Keyframe[]>; // every lane present, at least one keyframe each
}

export type MixTechnique = Technique | "filter sweep" | "loop roll" | "slam";

export interface MixMarker {
  beat: number;
  label: string; // "B in", "bass swap", "A out", "echo throw", "loop", "tempo ramp" …
}

export interface MixPlan {
  version: number;
  folder: string;
  from_id: string;
  to_id: string;
  technique: MixTechnique;
  bars: 4 | 8 | 16 | 32; // blend length
  key_lock: boolean; // always true for now (settings may expose it later)
  window: { start_beat: number; end_beat: number }; // the rendered preview range (master beats)
  tempo: Keyframe[]; // master BPM over master beats
  a: DeckPlan;
  b: DeckPlan;
  markers: MixMarker[];
  notes: string[]; // plain-language explanation of the plan
  edited: boolean; // true = user-saved edits (from .seedbox/mixplans), false = freshly generated
}

export interface MixRender {
  render_id: string; // content hash of the plan + inputs (cache key)
  audio_url: string; // "/api/renders/<render_id>.m4a"
  duration_s: number;
  start_beat: number; // master beat at audio time 0 (== window.start_beat)
  beat_times_s: number[]; // audio time of every integer master beat from window.start_beat to window.end_beat
  lufs: number;
  true_peak_db: number;
  warnings: string[];
}
