# Seedbox 2 installers

`build_seedbox2.py` builds the installer for the machine it runs on. Run it on each target; `.github/workflows/installers.yml`
does that on GitHub's macOS arm64, macOS Intel and Windows runners. It needs [uv](https://docs.astral.sh/uv/), a network
connection, and:
- macOS: the Xcode command line tools, `brew install nasm pkgconf`.
- Windows: [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`choco install innosetup`).

```sh
uv run --no-project --python 3.13 packaging/build_seedbox2.py   # writes packaging/dist/seedbox2/
```

| | Result | Runs on |
|---|---|---|
| macOS arm64 | `Seedbox-2-<version>-macos-arm64.dmg` | macOS 13+ (the real minimum is measured and goes in `Info.plist`) |
| macOS Intel | `Seedbox-2-<version>-macos-x86_64.dmg` | macOS 13+ |
| Windows | `Seedbox-2-<version>-windows-x64-setup.exe` | Windows 10+, no administrator rights |
| Linux | only the app folder, for checking the build | |

Two separate Mac builds, not universal2: most wheels aren't universal, and two DMGs are each half the size.

**What is in the app** (`seedbox2.spec`): the portable CPython of a fresh venv with `requirements.txt` only, so no
torch, `frontend2/dist`, ffmpeg and deno. Model weights aren't bundled: the app downloads them on first run
(`seedbox2/models/`). The Windows setup installs into the user's folders; it adds a Start menu entry and, only when the
runtime is missing, runs Microsoft's WebView2 bootstrapper, which is checked by its signature at build time.

**Pins.** deno and ffmpeg are pinned by version and SHA-256 in `build_seedbox2.py` and `build_ffmpeg.sh`. Intel Macs get
`onnxruntime<1.24` from `requirements.txt`, the last release with Intel wheels, and the wheels picked for macOS 13 give
arm64 the same version, so both Macs run the same onnxruntime. `versions.txt` next to the installer lists what a build
took.

**ffmpeg stays LGPL.** imageio-ffmpeg's build, which runs Seedbox 2 from source, is GPL and is excluded. On macOS
`build_ffmpeg.sh` builds from source with libopus only, and refuses a result that says GPL. No one publishes an LGPL
build for macOS. On Windows it is BtbN's `win64-lgpl` build: if its release is ever pruned the download fails and the
pin needs a new month's build. The notice the licence asks for goes in the bundle's `licenses/` folder.

**Smoke test** (`check_seedbox2.py <executable>`): the build runs it on the built app and again on what the installer
installs (the DMG mounted, the setup installed and uninstalled). It runs `--self-test` with empty data folders and
requires the bundled ffmpeg and deno running, onnxruntime running an embedded tiny model in-process and in a spawned
worker, onnxruntime's accelerator in the bundle, pywebview's backend loading, the server serving the UI, no torch, and
ffmpeg with no GPL flag writing Opus. The build fails on an installer of 250 MB or more.

## Notarisation

Today's DMGs are ad-hoc signed, so macOS asks for a right-click → Open the first time (`README-seedbox2-macos.txt`
goes in the DMG). To ship them notarised, with an Apple Developer account:

1. Put a *Developer ID Application* certificate in the keychain.
2. `xcrun notarytool store-credentials seedbox2 --apple-id <id> --team-id <team>`.
3. Build with `SEEDBOX2_SIGN_IDENTITY="Developer ID Application: <name> (<team>)" SEEDBOX2_NOTARY_PROFILE=seedbox2`.

The build then signs the app with a hardened runtime and `seedbox2.entitlements`, signs the DMG, notarises it and
staples the ticket. This path hasn't run yet, as there is no account; expect to adjust the entitlements. Windows has
the same story: without a code signing certificate SmartScreen warns about the setup.
