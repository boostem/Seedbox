# MusicReco terminal tool (archived)

The interactive terminal tool that came before Seedbox. Nothing in the app imports it. It is kept here, as it was,
to be archived. To run it again, copy `tunebat_dl.py` back to the project root and `musicreco/audition.py` back
into `musicreco/`, then `.venv/bin/pip install -r _archive/requirements.txt`.

- `tunebat_dl.py`: the questionary + rich UI loop
- `musicreco/audition.py`: its 20 s clip player (the app uses `musicreco/previews.py`)
- `example_cloudscraper.py`: the cloudscraper demo the original spec pointed at
- `SPEC.md`: the original v1/v2 spec. Its engine contracts now live in the top-level `APP_SPEC.md`.
- `requirements.txt`: the extra packages it needs on top of the app's

---

# MusicReco: tunebat → yt-dlp DJ crate builder

An interactive terminal tool for building practice crates. You search [tunebat](https://tunebat.com), pick the
exact version of a track (the **seed**), and it ranks tunebat's recommended tracks by how well they mix with it.
You audition and tick the ones you want, and it downloads them from YouTube as tagged MP3s into a folder named
after the seed. The tracks are numbered in a suggested set order and rated for energy.

> This is a personal archival / DJ-practice tool. Only download what you have the right to, and buy the
> tracks you actually play out. The MP3s are re-encoded from YouTube's lossy audio, so they aren't club masters.

## Run

```sh
.venv/bin/python tunebat_dl.py
```

1. **Search**: type a search term. An empty search or Ctrl-C quits.
2. **Pick the seed**: use the arrow-key list of results (50 per page). Each row shows title, artists, album,
   release date, length, Camelot key and BPM, so you can tell the single, extended mix and remixes apart.
   Type to filter. *Next page*, *Previous page* and *New search* are at the bottom (↑ from the top jumps there).
3. **Recommendations**: a table of the seed and every tunebat recommendation, best match first, with its score,
   key, BPM, length and the reasons behind the score (see [Score and colours](../README.md#score-and-colours)).
4. **Checklist**: the same ranking as a checklist. By default every track starts ticked (Seedbox's **Pre-tick all
   recommendations** setting). With that setting off, the seed and every track that passes the mixing rule start
   ticked, and tracks that fail it start unticked, but you can tick them anyway. The line under the list gives the
   highlighted track's musical key, the rule verdict and the full reasons. Untick everything to go back to search.
   You're asked to confirm before anything downloads.

   | Key | Action |
   |---|---|
   | ↑ ↓ (or j k) | move |
   | space | tick / untick |
   | a | tick all (press again to untick all) |
   | i | invert the ticks |
   | p | play a 20 s clip of the highlighted track (see [Auditioning](#auditioning)) |
   | s | stop the clip |
   | ↵ | confirm |

5. **Download**: two tracks download at a time, with live progress. Next come a summary and the
   [set order](../README.md#set-order-and-energy). Then `tracks.csv` and `skipped.txt` are written and you're back at search.

Ctrl-C at any step returns to search. During downloads, Ctrl-C cancels the tracks that haven't started yet
and lets the running ones finish. Press Ctrl-C again to stop waiting.

## Auditioning

In the checklist, **p** plays a 20-second clip of the highlighted track, starting about 35% of the way in,
through `afplay`. The line under the list shows what's loading or playing, or why a clip couldn't be
played. The first play of a track takes a few seconds while the clip is found and fetched from YouTube. Clips are
cached for the session, so replays are instant. Pressing **p** on another row switches to that track, and **s** stops.
Playback stops when the checklist closes, and the cached clips are deleted when you quit.
