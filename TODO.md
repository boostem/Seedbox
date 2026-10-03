# Seedbox TODO

## Done (2026-10-03)
- [x] **Full set mix render**: the whole set as one continuous MP3 (every transition's MixPlan back to back, set-wide
      tempo continuity, .cue + .txt tracklist, ID3 chapters, progress in the UI, instant when nothing changed).
      `musicreco/setmix.py`, `seedbox/setmixes.py`, `frontend/src/features/setmix/`.
- [x] **Loops in transitions**: A's outro loops to stretch short outros into 16-bar blends, B's intro loops to hold
      its groove under a long outro, loop rolls (8 → 4 → 2 → 1 → ½) build into slams / quick cuts / drop swaps.
- [x] **Audible reverb, earlier echoes**: sends tap post-filter / pre-EQ, echo throws, reverb washes, new techniques
      brake and spinback. (Too much of it, on every transition: toned down and made a per-transition choice in
      round 2 below.)
- [x] **Blend multiple crates into a long setlist**: crate-to-crate links (with a "better opener" hint), setlists
      with drag order, auto-order, editable link transitions and a full-setlist mix render.
- [x] Unify pitch formatting (`fmtPitch` everywhere, zero = "±0%").
- [x] Waveform: cue labels keep clear of the INTRO/OUTRO labels.
- [x] "Order not saved" copy after a failed auto-order (the order didn't change; the toast now says so).
- [x] `.gitignore`'s `downloads` also hid `frontend/src/features/downloads/` (now `/downloads` only).

## Done (round 2: transitions that sound like a DJ picked them)
- [x] **Transition styles**: every transition has a style. The default is the **phrase mix** (B eases in and A eases
      out over whole phrases, bass swapped on the 1, no FX), the way most DJ mixes are built, and it repeats as often
      as it suits the songs. The technical styles (vocal throw, roll into the drop, echo exit, dub echo, swell in,
      wash exit, filter wash, filter sweep, loop roll, double drop, echo out, slam, quick cut, drop swap, brake,
      spinback) are scored on how well they fit the two songs (vocals, energy lift, what B lands on, keys, tempo
      jump) and the best fit wins. A technical style never runs more than 3 in a row; one used lately, or one that
      sounds like it (echo / wash / roll / turntable families), loses a little score so close calls vary; double
      drops, brakes and spinbacks are saved for every few transitions. Pick one yourself in the mix editor's Style
      menu; set mix tracklists show them. `mixplan.plan_styles`, `MixService.chain`.
- [x] **FX diet**: reverb washes and swells sit around -10 to -13 dB under the mix on dense tracks (they reached the
      level of the music before); a plain phrase mix carries no reverb and at most one echo (the vocal-clash throw).
- [x] **Loop-first moves**: A drops onto a clean loop of its own groove when the vocals would clash (its last word
      into the echo); radio edits get a looped 4-bar groove as a makeshift outro; A rolls its last bar into B's drop;
      double drops when two drops line up in key.

## Machine learning plan (all local)
Use ML for **perception** (what's in each track) and **ranking** (which plan is best), not to generate mixes end to
end: there's no clean data at that scale, it would be heavy to run on a laptop, and its output couldn't be edited in
the mix editor. In order of payoff:
- [ ] **Stems, no training needed**: run Demucs (htdemucs, MIT licence) once per track and cache per-bar stem energy
      under `.seedbox/`. Real vocal activity fixes most bad calls today (the heuristic hears synth leads as vocals,
      which misplaces clash ducking and echo throws); drums-only / bass-only bars sharpen intros, outros and bass
      swaps; the stems themselves enable acapella-over-instrumental blends later. About real time on a CPU, much
      faster on Apple Silicon.
- [ ] **Learn from real DJ mixes**: the DJ Mix Dataset (Kim et al., ISMIR 2020: 1,557 mixes and 13,728 tracks from
      1001Tracklists, `pip install djmix`, aligned with mix-to-track DTW) shows where DJs really mix in and out, how
      long transitions last and how tempo and key move; the same group's fader / EQ gain estimation (DAFx 2022)
      recovers the actual fader and EQ curves. Fit small models (gradient-boosted trees on our per-bar features) as
      priors for cue bars, overlap length and lane shapes. The audio comes from YouTube and no licence is stated, so
      this is for personal research only.
- [ ] **EDM structure and cue points**: Raveform (TISMIR, CC BY 4.0: beats, downbeats and EDM sections like
      buildup / breakdown / drop for ~1.4k tracks from DJ mixes) to train a section classifier that replaces the
      energy-based drop detection; the cue-point dataset from "Cue Point Estimation using Object Detection" (2024,
      ~21k expert cue points on ~5k tracks) for mix-in / mix-out points.
- [ ] **Learn the style chooser from you**: log each style / technique change and each saved edit with the pair's
      features, then fit a small reranker on top of the hand-scored table in `mixplan._flavor_choices` /
      `_exit_choices`. A few hundred choices are enough to learn "loves rolls into drops, skip the swells".
- [ ] **A render critic**: score rendered transitions from audio (beat drift, key clash, vocal over vocal via stems,
      loudness dips and bumps, FX level) and render 2-3 candidate plans per transition, keeping the best before you
      hear it.

## Next up (ideas)
- [ ] **Key shift for clashing keys**: transpose a deck ±1-2 semitones with Rubber Band (key lock already runs every
      deck through it) so a clash becomes a same-key / ±1 blend (e.g. 8A → 3A: B down a semitone = 8A). In a set
      the shift has to hold for the track's whole run, so the planner picks it per track, not per pair.
- [ ] **Cue points into rekordbox / Serato**: write the analysis' mix-in / mix-out / drop cues and the planned loops
      as memory + hot cues in the exported `.xml` (rekordbox POSITION_MARK) and Serato markers, so the set plays the
      same on CDJs.
- [ ] **"Build me a 60-minute set"** from the whole library: pick tracks across crates to hit a target length and
      energy arc (warm-up → peak → cool-down), then order and render it.
- [ ] **Stem-aware transitions** (on top of the cached stems in the ML plan): acapella-over-instrumental blends,
      vocal swaps, drums-only intros for tracks that don't have them.
- [ ] **A/B alternatives**: render 2-3 different plans for a transition side by side and keep the favourite.
- [ ] **Full-mix waveform** with transition markers and scrubbing on the set mix panel.
- [ ] **Publish a mix** to Mixcloud with its tracklist (their upload API takes sections), or SoundCloud.
- [ ] Render settings: target loudness, WAV/FLAC output, key lock off (vinyl-style pitch).
- [ ] Rip quality check: flag downloads with a low spectral cutoff or clipping before they reach a set.
- [ ] Until stems land, vocal detection stays heuristic: a lightweight harmonic/percussive split +
      pitch-salience-with-vibrato check would already cut the synth-lead false positives.
