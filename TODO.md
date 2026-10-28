# Seedbox TODO

## Next up
- [ ] **Full set mix render** (requested 2026-10-03): render the whole set as one continuous mix MP3 using every
      transition's MixPlan back to back, with set-wide tempo continuity (each track drifts imperceptibly toward the
      next meeting tempo), a tracklist (.txt + .cue with timestamps) and progress in the UI. Builds on
      `musicreco/mixplan.py` + `musicreco/mixrender.py`.

- [ ] **Use loops much more in transitions** (requested 2026-10-03): loop A's outro phrases to stretch short
      outros into long blends, loop rolls (8→4→2→1 beats) as builds before cuts, loop B's intro to hold a groove
      under A — the planner rarely uses DeckLoop today.
- [ ] **Reverb is almost never audible, echoes come too late** (feedback 2026-10-03): bring echo throws earlier /
      use them mid-blend (e.g. on A's vocal phrases), use reverb washes on breakdown exits and filter-sweep peaks.
- [ ] **Blend multiple crates into a long setlist** (requested 2026-10-03): suggest crate-to-crate links (e.g. "the
      end of this crate mixes well into the start of the Losing It or Apple crate") using transitions.partners
      across crates' last/first tracks; build and order a multi-crate setlist (pairs with the full set mix render).

## Later / ideas
- [ ] Vocal detection is heuristic — consider a better vocal-activity model.
