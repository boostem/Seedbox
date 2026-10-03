# Seedbox

Seedbox builds DJ crates. Pick a seed track and it lists tunebat's recommendations, scored and colour-coded for
harmonic mixing. You can preview any of them instantly. It downloads the ones you tick as tagged MP3s
(key, BPM, genre, energy) into one folder per seed, and the set builder puts them in a playable order.

**Requirements:** a Mac with Apple Silicon (M1 or newer), macOS {{MIN_MACOS}} or newer, and an internet connection.
On macOS 12 or 13, install the latest Safari update first (System Settings → Software Update): the Seedbox window
is drawn by Safari's engine, and an old one shows the interface wrongly.

## Install

1. Open `Seedbox-….dmg` and drag **Seedbox** onto the **Applications** folder.
2. Eject the disk image.
3. The first time, open Seedbox from Applications. The app isn't from the App Store or signed by an Apple
   developer account, so macOS blocks it the first time:
   - **macOS 15 (Sequoia) and newer:** double-click Seedbox. When macOS says it "could not verify" it, click
     **Done**. Then open **System Settings → Privacy & Security**, scroll down to the message about Seedbox,
     click **Open Anyway** and confirm with your password or Touch ID.
   - **Older macOS:** right-click (or Control-click) Seedbox in Applications, choose **Open**, then click
     **Open** again.
   - **Or with Terminal** (any macOS), run this once:

         xattr -dr com.apple.quarantine /Applications/Seedbox.app

   After that it opens normally.

## First run

- The window opens in the dark theme. Search for a track (or paste a Spotify link), pick the version you mean,
  and Seedbox shows its recommendations. Tracks that mix well with the seed are ticked for you.
- Previews (20-second clips) load in the background as soon as a crate opens. Click play and they start straight away.
- **Download** fetches the ticked tracks from YouTube Music as MP3 192 kbps and tags them.

## Where your files go

- Crates: `~/Music/MusicReco/<Artist - Seed title>/`, one folder per seed. Each holds the MP3s, `tracks.csv`,
  `skipped.txt`, and a `.m3u8` and rekordbox `.xml` playlist in set order. You can change the folder under
  **Settings**.
- Settings and downloaded updates: `~/Library/Application Support/Seedbox/`
- Preview cache (safe to delete): `~/Library/Caches/Seedbox/`
- Log file (send it to me if something breaks): `~/Library/Logs/Seedbox/seedbox.log`

## Keeping downloads working (yt-dlp updates)

YouTube changes often, and the downloader inside Seedbox (yt-dlp) needs updating every few weeks. Seedbox checks
for a new version each time it starts. When one is out, an **Update yt-dlp** button appears in Settings. Click it,
wait for the confirmation, then quit and reopen Seedbox. If downloads or previews suddenly stop working, try this
first.

## Troubleshooting

- **"Seedbox is damaged and can't be opened":** this is the quarantine flag. Run the `xattr` command above.
- **Nothing downloads / previews fail:** update yt-dlp (see above) and check your internet connection.
- **Diagnostics:** in Terminal, run
  `/Applications/Seedbox.app/Contents/MacOS/Seedbox --self-test` and send me the output.
- **Uninstall:** drag Seedbox to the Trash, and delete the folders listed above if you want. Your MP3s in
  `~/Music/MusicReco` stay where they are.

## Please note

Seedbox is a personal tool for DJ practice and archiving music you're entitled to use. It isn't a store or a
streaming service. Respect artists and rights holders: buy the tracks you play out (Beatport, Bandcamp, …), and
don't redistribute downloads. Search data comes from tunebat, and audio comes from YouTube via yt-dlp.
