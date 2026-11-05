/**
 * The numbers the live engine shares with the offline renderer (`musicreco/mixrender.py`). Change one here only with
 * its twin there: `tests/test_seedbox2_engine_parity.py` nulls the two engines against each other.
 */
export const SR = 44100;
export const CTL = 16; // control-rate decimation (samples)
export const BLOCK = 128; // the longest stretch the core renders in one go (a Web Audio render quantum)

export const STEP_RAMP_S = 0.01;
export const FILTER_SMOOTH_S = 0.03;
export const SEAM_S = 0.005;

export const XOVER_LOW_HZ = 200;
export const XOVER_HIGH_HZ = 2500;
export const FILTER_LP_MIN_HZ = 80;
export const FILTER_LP_MAX_HZ = 20000;
export const FILTER_HP_MIN_HZ = 20;
export const FILTER_HP_MAX_HZ = 10000;
export const FILTER_Q_BASE = 1.3066;
export const FILTER_Q_MAX = 5;
export const FILTER_RES_ONSET = 0.2;
export const FILTER_BLOCK = 64;

export const ECHO_RETURN = 1;
export const REVERB_RETURN = 0.7;
export const REVERB_BAND_HZ = [150, 9000] as const;

export const SPINBACK_SPEED = 3;
export const STUTTER_DUTY = 0.7;
export const FX_FADE_S = 0.003;

export const DECK_LUFS = -12;
export const LEVEL_THRESHOLD_DB = 1;
export const LEVEL_RATIO = 3;
export const MAKEUP_DB = 1;
export const CEILING_DBTP = -1.6;

/** Python's `round`: halves go to the even neighbour. The renderer rounds this way, and a sample off is a null lost. */
export function pyRound(x: number): number {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}
