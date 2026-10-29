"""Seedbox 2 evaluation: score the analysers against labelled real audio (`python -m seedbox2.eval`, #50).

- `datasets`: GiantSteps Tempo and Key, the Harmonix Set and our own corpus (`$SEEDBOX2_CORPUS`) as `Reference`s.
- `annotate`: export an analysis to an editable annotation (JSON or Rekordbox XML) and read corrections back.
- `metrics`: beat/downbeat F-measure, tempo accuracy, key score, boundary hit rate and vocal F, with `mir_eval`.
- `analysers`: the analysers under test, by name (`seedbox1` is the baseline).
- `run`: score analysers on datasets, print the table, save the run as JSON and check it against a baseline.
"""
