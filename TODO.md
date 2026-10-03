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
- [x] Double drops when two drops line up in key. (The loop-first moves from this round were taken back out in
      round 3: they sounded worse than plain blends.)

## Done (round 3: subtler, loop-free mixes)
- [x] **No loops in generated mixes**: every track plays straight through. Gone: stretching A's outro, holding B's
      intro, A jumping onto a loop of its own groove on a vocal clash (now: A's mids duck and its last word goes into
      the echo), the 4-bar looped "outros" for radio edits, and the stutters before slams, quick cuts, drop swaps
      and double drops. Loop rolls stay available by hand: the *Loop roll* technique and the *Roll* style.
- [x] **Subtler defaults**: the plain phrase mix needs a clear musical reason to give way to a flashier style, and
      echo outs ring a bit quieter and shorter.

## Done (round 4: echoes you can hear, presets that adapt)
- [x] **Audible echoes**: throws capture exactly A's last beat (the send opens on it and closes on the 1), a louder
      return, ping-pong repeats, a steep band-limit on the way in and only gentle filtering in the loop, the delay
      time from the tempo (dotted 8th, an 8th on slow songs, a quarter on fast ones), the tail sized to end before
      B's next vocal or drop (about 2-8 repeats), only two repeats when A's last word would land on B's vocal, and
      B's mids dipping while the first repeats ring. First second after the cut, 300 Hz-5 kHz, against B: 9 to
      13 dB louder than before.
- [x] **Adaptive presets**: every plan carries `style_fit` (each alternative's fit for these two songs and why);
      the Style / Technique menus are ranked and labelled by it, strong alternatives show as one-click chips, and
      a note says when the best fit stepped aside for variety. Preset settings come from the songs (echo time, send,
      repeats and B's dip; swell and wash levels from how sparse the intro / ending are; the filter climb from the
      blend length).

## Machine learning plan (researched 2026-10-03)
What the research says: no released model predicts a whole transition plan from audio (the one learned transition
generator, DJtransGAN, rated no better than rules in its own listening test), and commercial auto-mixers (djay
Automix AI, rekordbox, Engine DJ, VirtualDJ, DJ.Studio, Spotify Mix, Apple Music AutoMix) adapt where and how long
to mix, plus stem gains, but keep FX choice and settings fixed or random. So: use ML to **hear the tracks better**
and to **rank plans**, keep the planner's explainable rules, and let real-mix data and your edits tune them.

**Phase 1, an optional "ML pack"** (an app variant or add-on, est. +450-520 MB: PyInstaller can't install packages
at run time). torch 2.11 is the newest that still runs on the app's macOS 12 floor with Python 3.14.
- [ ] **Demucs htdemucs** (MIT, 84 MB weights; est. ~8 s per 4-min track on the Apple GPU, ~1.5x track length on a
      CPU): cache per-bar vocal / drum / bass stem loudness. Real vocal activity replaces the heuristic that hears
      synth leads as vocals, echo throws land on the true last vocal offset, and drums-only bars become known. A
      published listening test (Mosaikbox, ISMIR 2024) rated stem-aware transitions 3.46 vs 3.08 (p < 0.0001).
- [ ] **Beat This!** (CPJKU, MIT code and weights, 8-78 MB): a per-beat grid with downbeats instead of a constant
      tempo, so phrases, throws and any future loops stay on the beat.
- [ ] **pyloudnorm** (MIT, tiny): per-bar loudness for level decisions (echo sends, B's dip, wash levels) instead of
      the per-track energy scale.

**Phase 2** (needs a macOS 14 floor for onnxruntime):
- [ ] **Discogs-EffNet** on onnxruntime (CC BY-NC-SA weights, ~22 MB + ~78 MB runtime): genre / style, voice vs
      instrumental and mood tags as preset priors (techno EQ blends, hip-hop / pop echo outs and cuts, DnB / dubstep
      double drops, dub echoes, trance long blends and washes).
- [ ] **allin1 v2 trained on Raveform** when it ships: EDM sections (buildup / drop / breakdown) at downbeat F1 0.965
      vs 0.753 for the pop-trained model.
- [ ] **CUE-DETR** (MIT, 166 MB): a second opinion on mix-in / mix-out cue points.

**Data and learning (no runtime dependencies)**:
- [ ] **Raveform priors** (CC BY 4.0, 479 MB of JSON: 4,902 real mixes, 53,780 transitions with beat-level
      alignment, EDM section labels on 1,423 tracks): real overlap lengths and which sections DJs enter and leave on,
      as a prior term in the planner's placement score.
- [ ] **Learn from your edits**: log every style / technique override and saved edit as (chosen, rejected) with the
      pair's features and fit small per-user weights on top of `_flavor_choices` / `_exit_choices` (DJ-MC is the
      precedent), so the ranking learns your taste.
- [ ] **Fader / EQ curve templates** from real mixes (Kim, Yang and Nam, DAFx 2022: a convex fit recovers fader and
      3-band EQ curves from a mix and its tracks): cluster them into lane templates and pick by song features.
      Research-only audio; ship only the aggregate templates.
- [ ] **A render critic**: render the top 2-3 plans and tie-break with an audio quality model
      (audiobox-aesthetics, CC BY 4.0), checked against listening first.

**Skip**: DJtransGAN as a generator, MusicGen-style generated bridges (non-commercial weights, GPU), Spleeter /
Open-Unmix (old, lower quality), madmom / BeatNet (broken installs, non-commercial weights), allin1 v1 (pop labels,
NATTEN build issues), CLAP / MERT, TensorFlow builds (0.4-1.1 GB), and bundling Essentia's AGPL library.

## Next up (ideas)
- [ ] **Key shift for clashing keys**: transpose a deck ±1-2 semitones with Rubber Band (key lock already runs every
      deck through it) so a clash becomes a same-key / ±1 blend (e.g. 8A → 3A: B down a semitone = 8A). In a set
      the shift has to hold for the track's whole run, so the planner picks it per track, not per pair.
- [ ] **Loops, done right (later)**: generated loops sounded off for two reasons: a constant-tempo beat grid drifts
      over the bars between a loop's source and where it plays, and the vocal heuristic let melodic or sung bars into
      loops. Revisit only with per-beat grids (downbeat tracking) and stems, and keep loops to drums-only material.
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
