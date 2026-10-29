"""Analysis: per-track data computed by jobs and stored in the library (#43–#49).

- `waveform`: 3-band peak and RMS at 150 points/s plus overviews, as one small binary artifact (#49)

Each module registers its job types (`seedbox2.jobs.register`) on import; `seedbox2.server.routers` imports them.
"""
