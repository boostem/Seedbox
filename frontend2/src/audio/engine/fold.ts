/**
 * A deck without stems plays its stem lanes on its EQ (`fold_stem_lanes` in `seedbox2/mix/plan.py`, whose output this has
 * to match: the parity test compares them). Bass goes on the lows, vocals on the mids, drums on the highs; "other" has no
 * band and stays up. A silent stem becomes an EQ kill, which is -30 dB and not silence.
 */
import { LANE_RANGES, STEM_EQ_FALLBACK, STEM_LANES } from "../../api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "../../api/mixplan.generated";
import { Lane } from "./clock";

const FOLD_STEP = 0.25;
const FOLD_MAX_POINTS = 1024;
const LEFT = 1e-6;
const SAME_DB = 1e-3;
const MAX_NOTES = 64;

function gainDb(gain: number): number {
  return 20 * Math.log10(Math.max(gain, 1e-6));
}

function foldPoints(eq: Lane, gain: Lane): [number, boolean][] {
  const knots = [...new Set([...eq.beats, ...gain.beats])].sort((a, b) => a - b);
  const moving: boolean[] = [];
  for (let i = 0; i + 1 < knots.length; i++) {
    const [b0, b1] = [knots[i], knots[i + 1]];
    moving.push([eq, gain].some((lane) => Math.abs(lane.at(b0) - lane.at(b1 - LEFT)) > 1e-9));
  }
  let total = 0;
  for (let i = 0; i + 1 < knots.length; i++) if (moving[i]) total += knots[i + 1] - knots[i];
  const step = Math.max(FOLD_STEP, total / FOLD_MAX_POINTS);
  const points: [number, boolean][] = [];
  for (let i = 0; i + 1 < knots.length; i++) {
    const [b0, b1, m] = [knots[i], knots[i + 1], moving[i]];
    const n = m ? Math.ceil((b1 - b0) / step) : 1;
    for (let j = 0; j < n; j++) points.push([b0 + ((b1 - b0) * j) / n, m]);
  }
  points.push([knots[knots.length - 1], false]);
  return points;
}

function foldLane(eq: Keyframe[], gain: Keyframe[], lo: number, hi: number): Keyframe[] {
  const eqLane = new Lane(eq);
  const gainLane = new Lane(gain);
  const db = (beat: number): number => Math.min(hi, Math.max(lo, eqLane.at(beat) + gainDb(gainLane.at(beat))));
  const out: Keyframe[] = [];
  for (const [beat, moves] of foldPoints(eqLane, gainLane)) {
    const arrive = db(beat - LEFT);
    const value = db(beat);
    if (out.length && out[out.length - 1].curve !== "hold" && Math.abs(arrive - value) > SAME_DB) {
      out.push({ beat, value: arrive, curve: "linear" });
    }
    out.push({ beat, value, curve: moves ? "linear" : "hold" });
  }
  return out;
}

function foldDeck(deck: Deck): Deck {
  const lanes: Lanes = { ...deck.lanes };
  const stems = deck.stem_lanes!;
  for (const [stem, band] of Object.entries(STEM_EQ_FALLBACK) as [keyof typeof STEM_EQ_FALLBACK, keyof Lanes][]) {
    const gain = stems[stem];
    if (gain.some((k) => k.value !== 1)) {
      const [lo, hi] = LANE_RANGES[band];
      lanes[band] = foldLane(deck.lanes[band], gain, lo, hi);
    }
  }
  return { ...deck, lanes, stem_lanes: null };
}

/** The plan as it plays on these tracks, saying so in its notes when that changes what a deck does. */
export function foldStemLanes(plan: MixPlan, hasStems: (trackId: string) => boolean): MixPlan {
  const update: Partial<Pick<MixPlan, "a" | "b">> = {};
  const notes = [...plan.notes];
  for (const name of ["a", "b"] as const) {
    const deck = plan[name];
    if (deck.stem_lanes === null || hasStems(deck.track_id)) continue;
    update[name] = foldDeck(deck);
    if (STEM_LANES.some((lane) => deck.stem_lanes![lane].some((k) => k.value !== 1))) {
      notes.push(`Deck ${name.toUpperCase()} has no stems: its stem lanes play on its EQ.`);
    }
  }
  return Object.keys(update).length ? { ...plan, ...update, notes: notes.slice(-MAX_NOTES) } : plan;
}
