# Seedbox and Setbox

Two apps that work on the same crates folder:

- **Seedbox** builds DJ crates. Pick a seed track and it lists tunebat's recommendations, scored and colour-coded
  for harmonic mixing. You can preview any of them instantly. It downloads the ones you tick as tagged MP3s (key,
  BPM, genre, energy) into one folder per seed.
- **Setbox** turns those crates into sets. It analyses the songs, suggests a playable order and a transition for
  every pair, splits vocals so voices don't clash, and renders the transitions or the whole set as one mix. It also
  chains crates into longer setlists.

**Requirements:** a Mac with Apple Silicon (M1 or newer), macOS {{MIN_MACOS}} or newer, and an internet connection.
On macOS 12 or 13, install the latest Safari update first (System Settings → Software Update): the windows are
drawn by Safari's engine, and an old one shows the interface wrongly.

## Install

1. Open `Seedbox-….dmg` and drag **Seedbox** and **Setbox** onto the **Applications** folder.
2. Eject the disk image.
3. The first time, open each app from Applications. They aren't from the App Store or signed by an Apple
   developer account, so macOS blocks each one the first time:
   - **macOS 15 (Sequoia) and newer:** double-click the app. When macOS says it "could not verify" it, click
     **Done**. Then open **System Settings → Privacy & Security**, scroll down to the message about the app,
     click **Open Anyway** and confirm with your password or Touch ID.
   - **Older macOS:** right-click (or Control-click) the app in Applications, choose **Open**, then click
     **Open** again.
   - **Or with Terminal** (any macOS), run this once:

         xattr -dr com.apple.quarantine /Applications/Seedbox.app /Applications/Setbox.app

   After that they open normally.

## First run

- **Seedbox:** search for a track (or paste a Spotify link), pick the version you mean, and Seedbox shows its
  recommendations. Tracks that mix well with the seed are ticked for you. Previews (20-second clips) load in the
  background as soon as a crate opens. Click play and they start straight away. **Download** fetches the ticked
  tracks from YouTube Music as MP3 192 kbps and tags them.
- **Setbox:** open a crate. Setbox analyses its songs (a few seconds each) and shows them in a suggested order
  with a transition between each pair. Play a transition to hear it, or render the whole set as one MP3. The first
  time it splits vocals it downloads a vocal model (about 200 MB), then works through your songs in the background.

You can keep both apps open: download in Seedbox, and the new songs show up in Setbox the next time you open
that crate.

## Where your files go

- Crates: `~/Music/MusicReco/<Artist - Seed title>/`, one folder per seed. Each holds the MP3s, `tracks.csv`,
  `skipped.txt`, and a `.m3u8` and rekordbox `.xml` playlist in set order. Setbox keeps each crate's song analyses
  and split vocals in a hidden `.seedbox` folder inside it. You can change the folder under **Settings** in either
  app (it's the same setting).
- Full mixes from Setbox: `~/Music/MusicReco/Mixes/<name>/` (the MP3, a `.cue` sheet and a tracklist)
- Settings, the vocal model and downloaded updates: `~/Library/Application Support/Seedbox/`
- Preview clips and rendered transitions (safe to delete): `~/Library/Caches/Seedbox/`
- Log files (send them to me if something breaks): `~/Library/Logs/Seedbox/seedbox.log` and
  `~/Library/Logs/Setbox/setbox.log`

## Keeping downloads working (yt-dlp updates)

YouTube changes often, and the downloader inside Seedbox (yt-dlp) needs updating every few weeks. Seedbox checks
for a new version each time it starts. When one is out, an **Update yt-dlp** button appears in Settings. Click it,
wait for the confirmation, then quit and reopen Seedbox. If downloads or previews suddenly stop working, try this
first.

## Troubleshooting

- **"Seedbox is damaged and can't be opened"** (or Setbox): this is the quarantine flag. Run the `xattr` command
  above.
- **Nothing downloads / previews fail:** update yt-dlp (see above) and check your internet connection.
- **Vocal splitting waits:** Setbox needs the internet once to download the vocal model. It retries on its own;
  transitions still work meanwhile, with vocals handled by EQ.
- **Diagnostics:** in Terminal, run
  `/Applications/Seedbox.app/Contents/MacOS/Seedbox --self-test` (or
  `/Applications/Setbox.app/Contents/MacOS/Setbox --self-test`) and send me the output.
- **Uninstall:** drag Seedbox and Setbox to the Trash, and delete the folders listed above if you want. Your MP3s
  in `~/Music/MusicReco` stay where they are.

## Please note

Seedbox is a personal tool for DJ practice and archiving music you're entitled to use. It isn't a store or a
streaming service. Respect artists and rights holders: buy the tracks you play out (Beatport, Bandcamp, …), and
don't redistribute downloads. Search data comes from tunebat, and audio comes from YouTube via yt-dlp.
