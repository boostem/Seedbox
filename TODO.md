# Seedbox TODO

## Done (2026-10-03)
- [x] **Full set mix render**: the whole set as one continuous MP3 (every transition's MixPlan back to back, set-wide
      tempo continuity, .cue + .txt tracklist, ID3 chapters, progress in the UI, instant when nothing changed).
      `musicreco/setmix.py`, `seedbox/setmixes.py`, `frontend/src/features/setmix/`.
- [x] **Loops in transitions**: A's outro loops to stretch short outros into 16-bar blends, B's intro loops to hold
      its groove under a long outro, loop rolls (8 → 4 → 2 → 1 → ½) build into slams / quick cuts / drop swaps.
- [x] **Audible reverb, earlier echoes**: sends tap post-filter / pre-EQ (reverb went from ~-27 dB to ~-5 dB under
      the mix), echo throws on A's vocal phrase ends, vocal clashes and the beat before the bass swap, reverb washes
      on filter-sweep peaks, breakdown exits and drops. New techniques: brake, spinback.
- [x] **Blend multiple crates into a long setlist**: crate-to-crate links (with a "better opener" hint), setlists
      with drag order, auto-order, editable link transitions and a full-setlist mix render.
- [x] Unify pitch formatting (`fmtPitch` everywhere, zero = "±0%").
- [x] Waveform: cue labels keep clear of the INTRO/OUTRO labels.
- [x] "Order not saved" copy after a failed auto-order (the order didn't change; the toast now says so).
- [x] `.gitignore`'s `downloads` also hid `frontend/src/features/downloads/` (now `/downloads` only).

## Next up (ideas)
- [ ] **Key shift for clashing keys**: transpose a deck ±1-2 semitones with Rubber Band (key lock already runs every
      deck through it) so a clash becomes a same-key / ±1 blend (e.g. 8A → 3A: B down a semitone = 8A). In a set
      the shift has to hold for the track's whole run, so the planner picks it per track, not per pair.
- [ ] **Cue points into rekordbox / Serato**: write the analysis' mix-in / mix-out / drop cues and the planned loops
      as memory + hot cues in the exported `.xml` (rekordbox POSITION_MARK) and Serato markers, so the set plays the
      same on CDJs.
- [ ] **"Build me a 60-minute set"** from the whole library: pick tracks across crates to hit a target length and
      energy arc (warm-up → peak → cool-down), then order and render it.
- [ ] **Stem-aware transitions** (Demucs separation, cached per track): acapella-over-instrumental blends, vocal
      swaps, drums-only intros for tracks that don't have them. Would also replace the vocal heuristic below.
- [ ] **A/B alternatives**: render 2-3 different plans for a transition side by side and keep the favourite.
- [ ] **Full-mix waveform** with transition markers and scrubbing on the set mix panel.
- [ ] **Publish a mix** to Mixcloud with its tracklist (their upload API takes sections), or SoundCloud.
- [ ] Render settings: target loudness, WAV/FLAC output, key lock off (vinyl-style pitch).
- [ ] Rip quality check: flag downloads with a low spectral cutoff or clipping before they reach a set.
- [ ] Vocal detection is heuristic: a lightweight harmonic/percussive split + pitch-salience-with-vibrato model,
      or stems (above), would make vocal-clash avoidance much more reliable.
