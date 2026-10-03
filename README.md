# Seedbox (desktop app)

Seedbox is the desktop version of this tool. It runs the same engine (`musicreco/`) in a dark, DJ-style window.
Search tunebat, open a seed's crate (20 s previews of every rec load in the background), tick and download. Then
build the set: drag to reorder, with waveforms, intro/outro and mix-point analysis and suggested transitions. Under the
hood it's a FastAPI server (`seedbox/`) on 127.0.0.1, with the React UI (`frontend/`) shown in a native pywebview window.

### Run from source

Do the [Setup](#setup) below first (`.venv`, Homebrew `ffmpeg` + `deno`).

```sh
(cd frontend && deno install && deno task build)   # once, and after frontend changes (writes frontend/dist)
.venv/bin/python -m seedbox                        # opens the app window
```

To work on the UI with hot reload, use two terminals:

```sh
cd frontend && deno task dev          # Vite on http://localhost:5173, proxies /api to 127.0.0.1:8765
.venv/bin/python -m seedbox --dev     # API on :8765; the window loads the Vite page
```

Other flags:
- `--server-only [--port N]`: no window. Open the printed URL in a browser.
- `--self-test [--self-test-clip]`: JSON diagnostics covering ffmpeg/deno, a YouTube Music search and the deno JS-challenge probe.
- `--update-ytdlp`
- `--debug`

`.venv/bin/python -m seedbox.server --port 8765 --dev` runs the bare API (no window, no built UI).

### Transitions, full mixes and setlists

- **Transitions you can hear.** Every pair in the set builder has a rendered transition with editable automation
  (fader, 3-band EQ, filter, echo, reverb, loops, tempo). Most are **phrase mixes**: B eases in and A eases out over
  whole phrases with the bass swapped on the 1, like most DJ mixes. Where a pair suits something flashier it gets it:
  a **vocal throw** when A sings out over B's instrumental, a reverb **swell** for a pad intro, a **double drop**
  when two drops line up, a **brake** into a quiet intro, a **spinback** over a big tempo jump, and more. Each is the
  best fit for its two songs, and no flashy style runs more than three times in a row. Every track plays straight
  through (nothing is looped), and when two vocals would clash the outgoing one is ducked and thrown into the echo.
  Loop rolls only happen if you pick *Loop roll* or the *Roll* style in the mix editor.
- **Presets that adapt to the songs.** The mix editor's Style menu ranks the presets for the two songs in front of
  it ("great fit", "good fit", "won't fit") with the reason, and suggests the other strong options as one-click
  chips. Picking a preset doesn't paste fixed settings: the echo time follows the tempo, the echo's level follows how
  loud B comes in (B's mids dip so the repeats cut through), its repeats ring out before B's next vocal or drop,
  throws land on A's last sung word, and swells, washes and filter climbs are sized to the intro, ending and blend.
- **Full mix.** *Full mix* on the set builder renders the whole set as one continuous MP3 (320 kbps) using every
  transition exactly as planned (saved edits included), with tempo moves spread across each track so the set flows.
  It lands in `<crates folder>/Mixes/<name>/` with a `.cue` sheet, a `.txt` tracklist and ID3 chapters. Rendering
  runs in the background (about a minute per 10 minutes of music on Apple Silicon); re-rendering an unchanged set is
  instant.
- **Setlists.** Chain crates into one long set (*Setlists*, ⌘3, or *Link crates* on a set builder, which ranks how
  well this crate's ending mixes into every other crate's start). Each crate keeps its own running order; the links
  between crates get their own editable transitions, *Auto-order crates* finds the best chain, and *Full mix* renders
  the whole setlist.

### Build the app to share

```sh
brew install uv                       # once
packaging/build.sh --release          # → packaging/dist/Seedbox.app + Seedbox-<version>-arm64.dmg
```

`--release` builds against a portable CPython in `packaging/.relvenv` (uv-managed, with package versions pinned
to `.venv`). It bundles ffmpeg and deno, ad-hoc signs the app, runs a headless smoke test and writes the DMG. The
app's minimum macOS (`LSMinimumSystemVersion`) is computed from the bundled binaries. Without `--release`, the build
uses `.venv`'s Homebrew Python, and the app only runs on this Mac's macOS version or newer. Apple Silicon only.

### Where things live

| What | Where |
|---|---|
| Crates: MP3s, `tracks.csv`, `skipped.txt`, `.xml`, `.m3u8` | `~/Music/MusicReco/<Artists - Title of seed>/` (change it under Settings). The project's `downloads/` is a symlink to it: the first launch from source moved the old crates there. |
| Crate metadata, analysis cache | `<crate>/.seedbox/` (`crate.json`, `analysis/<track id>.json`) |
| Settings, downloaded yt-dlp updates | `~/Library/Application Support/Seedbox/` (`settings.json`, `pydeps/`) |
| Rendered set mixes (MP3 + .cue + .txt) | `<crates folder>/Mixes/<name>/` (not a crate; index in `<crates folder>/.seedbox/setmixes.json`) |
| Setlists | `<crates folder>/.seedbox/setlists/<id>.json` (+ `<id>/mixplans/` for edited crate-to-crate links) |
| Preview clips (safe to delete) | `~/Library/Caches/Seedbox/previews/` (capped at 500 MB by default) |
| Log | `~/Library/Logs/Seedbox/seedbox.log` |

For tests and scratch runs, `MUSICRECO_OUTPUT_ROOT` overrides the crates folder (it's never saved).
`SEEDBOX_SUPPORT_DIR`, `SEEDBOX_CACHE_DIR` and `SEEDBOX_LOG_DIR` move the other folders. The terminal tool below
reads the same settings and writes to the same crates folder.

### Sharing the .dmg

Send friends the `.dmg` from `packaging/dist/`. It isn't signed with an Apple developer account, so macOS blocks
the first launch. [packaging/README-friends.md](packaging/README-friends.md) covers installing, opening it the first
time, where files go and updating yt-dlp from Settings. It's included in the DMG as `Read Me First.txt`.

---

# MusicReco: tunebat → yt-dlp DJ crate builder

An interactive terminal tool for building practice crates. You search [tunebat](https://tunebat.com), pick the
exact version of a track (the **seed**), and it ranks tunebat's recommended tracks by how well they mix with it.
You audition and tick the ones you want, and it downloads them from YouTube as tagged MP3s into a folder named
after the seed. The tracks are numbered in a suggested set order and rated for energy.

> This is a personal archival / DJ-practice tool. Only download what you have the right to, and buy the
> tracks you actually play out. The MP3s are re-encoded from YouTube's lossy audio, so they aren't club masters.

## Setup

```sh
brew install ffmpeg deno          # ffmpeg for MP3 encoding + loudness, deno for yt-dlp's YouTube JS challenges
/opt/homebrew/bin/python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Run

```sh
.venv/bin/python tunebat_dl.py
```

1. **Search**: type a search term. An empty search or Ctrl-C quits.
2. **Pick the seed**: use the arrow-key list of results (50 per page). Each row shows title, artists, album,
   release date, length, Camelot key and BPM, so you can tell the single, extended mix and remixes apart.
   Type to filter. *Next page*, *Previous page* and *New search* are at the bottom (↑ from the top jumps there).
3. **Recommendations**: a table of the seed and every tunebat recommendation, best match first, with its score,
   key, BPM, length and the reasons behind the score (see [Score and colours](#score-and-colours)).
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
   [set order](#set-order-and-energy). Then `tracks.csv` and `skipped.txt` are written and you're back at search.

Ctrl-C at any step returns to search. During downloads, Ctrl-C cancels the tracks that haven't started yet
and lets the running ones finish. Press Ctrl-C again to stop waiting.

## The mixing rule

With **Pre-tick all recommendations** off, a recommendation is pre-ticked only when **both** of these hold:

- **Key**: it's adjacent on the Camelot wheel: the same key, ±1 with the same letter (12 ↔ 1 wraps), or
  the same number with the other letter (relative major/minor). For example, from `1A` the matches are
  `1A 12A 2A 1B`.
- **BPM**: it's within ±10% of the seed, counting half and double time. For example, a 65 BPM track matches a 130 BPM seed.

## Score and colours

Every recommendation gets a **score from 0 to 100** that ranks the table and the checklist. The score combines
the key move, how far the tempo has to be pitched (half/double time counts, with a small penalty), and how
close the energy and danceability are to the seed. Tracks that pass the rule always score 48 or more, and
tracks that fail it always score lower, so rule matches are always on top. The rule decides the ticks (when
**Pre-tick all recommendations** is off). The score just sets the order.

The score and the key are coloured by the **Camelot move** from the seed:

| Colour | Move |
|---|---|
| green | same key |
| cyan | ±1 on the wheel |
| yellow | relative major/minor (same number, other letter) |
| magenta | energy boosts and diagonals: +2, +7, or ±1 with the other letter |
| red | clash (anything else) |

Rows that fail the rule are dimmed, and an unknown key shows as `?`. The **Why** column lists the reasons
behind the score, for example `relative · -1.5% pitch · energy +0.07` (pitch is the tempo change needed to match
the seed).

## Auditioning

In the checklist, **p** plays a 20-second clip of the highlighted track, starting about 35% of the way in,
through `afplay`. The line under the list shows what's loading or playing, or why a clip couldn't be
played. The first play of a track takes a few seconds while the clip is found and fetched from YouTube. Clips are
cached for the session, so replays are instant. Pressing **p** on another row switches to that track, and **s** stops.
Playback stops when the checklist closes, and the cached clips are deleted when you quit.

## Set order and energy

After each download, every track in the batch is given an **energy rating from 1 to 10**. The ratings are
relative to the crate and come from tunebat's energy, loudness, danceability and happiness values. The tracks are also
arranged into a **running order** that starts with the seed and keeps key moves and BPM drift small. Energy
builds gently to a peak about two-thirds of the way through, then eases off. The order is printed as a table
with each track's number, Camelot key, BPM and energy stars. Each key is coloured by the move from the track
before it. Two small curves show how energy and BPM change across the set.

- **Track numbers**: the order is written to each file's track number (`TRCK`, e.g. `3/12`), so sorting by
  track number in your DJ software plays the crate in that order.
- **Playlist files** (rewritten after every run), for DJ apps that ignore track numbers:
  - `<seed>.xml` — the crate as a playlist in rekordbox's XML exchange format (no rekordbox needed), with key,
    BPM, genre, label and energy stars. **AutoPilot:** import this file (rekordbox XML import, with playlist
    import on) instead of the folder — its folder import orders tracks by analysis finish time and ignores
    track numbers, M3U files and star tags, but its XML import keeps the playlist order.
  - `<seed>.m3u8` — a plain playlist in set order for apps and players that read M3U.
- **Energy stars**: energy 1–10 becomes 1–5 stars (rating ÷ 2, rounded up), written as `POPM` rating frames.
  Ratings aren't standardised, so one frame is written per reader: Serato DJ 4 and Windows Explorer read the
  common one, Traktor reads its own (after *Check Consistency*), and rekordbox picks the rating up only when a
  track is first imported. Apple Music/Finder, VirtualDJ, Engine DJ and Mixxx ignore it.

The whole crate is rated and numbered after every run, including files left in the seed folder by earlier runs
that you didn't tick this time, so track numbers never collide.

## Output

```
~/Music/MusicReco/               # the crates folder from Seedbox's settings (the project's downloads/ links here)
  <Artists - Title of seed>/
    <Artists - Title>.mp3        # MP3 192 kbps
    <Artists - Title of seed>.xml   # the crate in set order, rekordbox XML format (import into AutoPilot)
    <Artists - Title of seed>.m3u8  # the crate in set order, plain playlist
    tracks.csv                   # seed + every recommendation: score, rule, ticked?, status, genre, Beatport, loudness…
    skipped.txt                  # appended each run: skipped/failed tracks + the closest YouTube candidate
```

- **Matching**: YouTube Music is searched first and regular YouTube is the fallback. A candidate is accepted
  only if its length is within ±15 s of the Spotify length. If no candidate qualifies, the track is skipped and
  logged in `skipped.txt` rather than guessed. Downloads that fail on a temporary error (rate limits, timeouts,
  broken fragments) get up to 3 attempts. If the best candidate still won't download, the other candidates
  that fit the ±15 s window are tried in order.
- **No half-finished files**: each track is downloaded, analysed and tagged in a hidden `.tmp-…` folder and
  only moved into place once complete. Leftovers from a crashed run are cleaned up next time.
- **Already have it**: a track already in this seed's folder (matched by Spotify ID) shows as `exists` and is
  left alone. If it's in another seed's folder, it's **copied** over and retagged, with no YouTube download.
- When two tracks have the same "Artists - Title" (for example a single and an album version), the second
  file gets a short ID suffix: `Name [4uLU6h].mp3`.

### tracks.csv

One row per track: the seed and every recommendation, ticked or not. The columns include the score and Camelot
move, the rule result, tunebat's key/BPM/energy, the outcome (status, file, YouTube URL, reason), genre and
source, energy rating and set position, loudness (LUFS and ReplayGain), and the Beatport details: URL, label,
catalog number, ISRC, mix name, and Beatport's own key, Camelot and BPM.

`key_mismatch` / `bpm_mismatch` are `yes` when Beatport disagrees with tunebat. Keys mismatch when the Camelot
codes differ. BPMs mismatch when they're more than 2% apart even after allowing for half/double time. Tags always
use tunebat's key and BPM, so these columns are where to look when a track sounds off.

**Re-running a seed merges** into the existing `tracks.csv` instead of replacing it. Rows are matched by Spotify
ID, and this run's rows come first. A track with no new outcome keeps its earlier status, file and details.
That covers tracks you left unticked this time, and tracks that failed or were skipped while their earlier file
is still in the folder. Rows for tracks that aren't in this run are kept, and so are any columns you added
yourself. The file is replaced atomically. If it can't be read, the old copy is saved as `tracks.csv.bak`.

## Tags (ID3v2.3)

Any tags from YouTube are removed first. Then these are written:

| Tag | Value |
|---|---|
| Title / Artist / Album / Year | from tunebat (Spotify metadata) |
| BPM | rounded tunebat BPM (never Beatport's) |
| Key (TKEY) | tunebat's Camelot code, e.g. `1A` (never Beatport's) |
| Grouping (TIT1) | the seed, `Artists - Title`, so a crate can be grouped in your DJ software |
| Genre | one specific genre, if found (see below) |
| Cover | 640 px Spotify album art |
| Track number (TRCK) | position in the set order, e.g. `3/12` |
| Rating (POPM) | energy as 1–5 stars |
| `REPLAYGAIN_TRACK_GAIN` / `_PEAK` | ReplayGain 2.0 loudness, e.g. `-3.21 dB` / `0.987654` |
| Publisher (TPUB) | label, from Beatport |
| ISRC (TSRC) | from Beatport |
| Subtitle (TIT3) | mix name, e.g. `Extended Mix`, from Beatport |
| Remixer (TPE4) | remixers, from Beatport |
| WOAF | Beatport track page |
| `TXXX:BEATPORT_TRACK_ID` / `TXXX:CATALOGNUMBER` | from Beatport |
| `TXXX:SPOTIFY_TRACK_ID` | Spotify ID, used to detect existing files and copies across seeds |

The Beatport tags are written only when Beatport has a confident match for the track.

**ReplayGain**: each MP3 is measured once with ffmpeg (EBU R128 integrated loudness and true peak). The gain
is the change needed to reach −18 LUFS. Players that honour ReplayGain (foobar2000, VLC and others) then play
every track at about the same loudness. The file's audio itself is never changed. rekordbox, Serato and Traktor
run their own auto-gain analysis and ignore these tags.

**Genre sources**: the tool scrapes Beatport first and uses the sub-genre if there is one, otherwise the genre.
If Beatport has no confident match, it falls back to MusicBrainz, using the recording's genres first and then
the artist's. The genre is left empty if neither source has one. `tracks.csv` records which source was used.
If Beatport or MusicBrainz is down or rate-limiting, that miss isn't cached as "no genre", so the next
lookup of the track tries again.

## Keeping yt-dlp working

YouTube changes often. If downloads start failing, update yt-dlp first:

```sh
.venv/bin/pip install -U "yt-dlp[default]"
```
