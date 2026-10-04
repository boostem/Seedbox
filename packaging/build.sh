#!/usr/bin/env bash
# Build Seedbox.app + Setbox.app and Seedbox-<version>-arm64.dmg holding both (Apple Silicon, unsigned / ad-hoc
# signed). Seedbox finds, previews, downloads and tags crates; Setbox builds sets, transitions and mixes from them.
#
#   packaging/build.sh [--app=seedbox|setbox] [--release] [--skip-frontend] [--placeholder-web] [--skip-smoke]
#                      [--skip-dmg]
#
# Steps: [release venv] → frontend (deno task build) → icons (if missing) → deno binary (Seedbox) → per app:
# PyInstaller (packaging/seedbox.spec with SEEDBOX_APP) → LSMinimumSystemVersion from every bundled Mach-O → ad-hoc
# codesign → smoke test (--server-only + /api/health + /api/system + its page, isolated temp dirs) → one DMG with
# the apps and an Applications link.
#
# --app=X    build only Seedbox.app or only Setbox.app (default: both). Seedbox.app has no PyTorch / analysis / mix
#            engine (a fraction of the size); Setbox.app has no deno.
# --release  build the one to share: a portable python-build-standalone CPython (uv-managed, built for macOS 11)
#            in $SEEDBOX_RELVENV (default packaging/.relvenv, created/updated with uv), wheels picked for
#            macOS $RELEASE_MACOS, versions pinned to what .venv has (pip freeze). Without --release the dev
#            .venv's Homebrew Python is used, which only runs on the build Mac's macOS version or newer.
#
# Env:
#   SEEDBOX_BUILD_DIR  output folder (default packaging/dist): Seedbox.app, *.dmg, .work/ (PyInstaller), .cache/
#   PYTHON             interpreter with the app's requirements + pyinstaller (default .venv/bin/python;
#                      --release sets it to the release venv)
#   SEEDBOX_RELVENV    release venv folder (default packaging/.relvenv)
#   RELEASE_PYTHON     CPython version for --release (default 3.14.7)
#   RELEASE_MACOS      oldest macOS whose wheels --release picks (default 12.0; deno/ffmpeg need 12.0 anyway)
#   RELEASE_TORCH      torch version --release bundles in Setbox.app (default 2.11.0: the newest with macOS 12 wheels for Python
#                      3.14; vocal splitting runs on the GPU on macOS 14+ and on the CPU before). The dev venv's
#                      torch isn't pinned, since newer wheels need macOS 14.
#   SEEDBOX_DENO       deno binary to bundle (default: Homebrew's if it only links system libraries, otherwise
#                      the official self-contained release for DENO_VERSION, downloaded + checksum-verified)
#   DENO_VERSION       e.g. 2.9.5 (default: the version of the deno on PATH, else the latest release)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$HERE")"
OUT="${SEEDBOX_BUILD_DIR:-$HERE/dist}"
WORK="$OUT/.work"
CACHE="$OUT/.cache"
PYTHON="${PYTHON:-$PROJECT/.venv/bin/python}"
SKIP_FRONTEND=0 PLACEHOLDER_WEB=0 SKIP_SMOKE=0 SKIP_DMG=0 RELEASE=0 APPS="seedbox setbox"
for arg in "$@"; do
  case "$arg" in
    --app=seedbox|--app=setbox) APPS="${arg#--app=}" ;;
    --release) RELEASE=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --placeholder-web) PLACEHOLDER_WEB=1 SKIP_FRONTEND=1 ;;
    --skip-smoke) SKIP_SMOKE=1 ;;
    --skip-dmg) SKIP_DMG=1 ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
app_name() { case "$1" in seedbox) echo Seedbox ;; setbox) echo Setbox ;; esac; }
app_page() { case "$1" in seedbox) echo index.html ;; setbox) echo setbox.html ;; esac; }
builds() { [[ " $APPS " == *" $1 "* ]]; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
system_libs_only() {  # true when a Mach-O links nothing outside /usr/lib and /System
  ! otool -L "$1" | tail -n +2 | awk '{print $1}' | grep -vqE '^(/usr/lib/|/System/Library/)'
}

[[ "$(uname -m)" == "arm64" ]] || die "build on an Apple Silicon Mac"

# ---------------------------------------------------------------- release venv (portable CPython)
if [[ $RELEASE == 1 ]]; then
  command -v uv >/dev/null || die "--release needs uv (brew install uv)"
  RELVENV="${SEEDBOX_RELVENV:-$HERE/.relvenv}"
  RELEASE_PYTHON="${RELEASE_PYTHON:-3.14.7}"
  RELEASE_MACOS="${RELEASE_MACOS:-12.0}"
  RELEASE_TORCH="${RELEASE_TORCH:-2.11.0}"
  step "release venv: uv-managed CPython $RELEASE_PYTHON in $RELVENV, wheels for macOS $RELEASE_MACOS"
  if [[ ! -x "$RELVENV/bin/python" ]] || ! "$RELVENV/bin/python" -c \
      "import sys; assert sys.version.split()[0] == '$RELEASE_PYTHON' and '/uv/python/' in sys.base_prefix"; then
    rm -rf "$RELVENV"
    uv venv --managed-python --python "$RELEASE_PYTHON" "$RELVENV"
  fi
  PINS=()
  if [[ -x "$PROJECT/.venv/bin/python" ]]; then  # same versions as the dev venv the app was tested with
    "$PROJECT/.venv/bin/python" -m pip freeze --exclude-editable | grep -viE '^torch==' > "$RELVENV/constraints.txt" \
      || true
    PINS=(-c "$RELVENV/constraints.txt")
  fi
  MACOSX_DEPLOYMENT_TARGET="$RELEASE_MACOS" uv pip install -q -p "$RELVENV/bin/python" \
    --python-platform aarch64-apple-darwin -r "$PROJECT/requirements.txt" "torch==$RELEASE_TORCH" pyinstaller \
    ${PINS[@]+"${PINS[@]}"}
  PYTHON="$RELVENV/bin/python"
fi

[[ -x "$PYTHON" ]] || die "no Python at $PYTHON"
"$PYTHON" -c "import PyInstaller, webview, uvicorn, fastapi, yt_dlp" 2>/dev/null \
  || die "$PYTHON lacks pyinstaller or the app's requirements"
mkdir -p "$OUT" "$WORK" "$CACHE"
BUILD_T0=$(date +%s)

# ---------------------------------------------------------------- frontend
if [[ $PLACEHOLDER_WEB == 1 ]]; then
  step "frontend: placeholder pages (--placeholder-web)"
  mkdir -p "$WORK/web-placeholder"
  for KEY in seedbox setbox; do
    NAME="$(app_name "$KEY")"
    cat > "$WORK/web-placeholder/$(app_page "$KEY")" <<HTML
<!doctype html><html data-app="$KEY"><head><meta charset="utf-8"><title>$NAME</title></head>
<body style="background:#0b0d10;color:#ddd;font:15px -apple-system;padding:48px">
<h2>$NAME</h2><p>Placeholder UI: this build was made without the frontend. The API runs at <code>/api</code>.</p>
</body></html>
HTML
  done
  export SEEDBOX_WEB_DIST="$WORK/web-placeholder"
elif [[ $SKIP_FRONTEND == 0 && -f "$PROJECT/frontend/package.json" ]]; then
  step "frontend: deno task build"
  command -v deno >/dev/null || die "deno is needed to build the frontend (brew install deno)"
  [[ -d "$PROJECT/frontend/node_modules" ]] || (cd "$PROJECT/frontend" && deno install)
  (cd "$PROJECT/frontend" && deno task build)
fi
if [[ -z "${SEEDBOX_WEB_DIST:-}" ]]; then
  for KEY in $APPS; do
    [[ -f "$PROJECT/frontend/dist/$(app_page "$KEY")" ]] \
      || die "no frontend/dist/$(app_page "$KEY") (build the frontend or pass --placeholder-web)"
  done
fi

# ---------------------------------------------------------------- icons
for KEY in $APPS; do
  if [[ ! -f "$HERE/icon/$(app_name "$KEY").icns" ]]; then
    step "icon: $(app_name "$KEY")"
    "$PYTHON" "$HERE/icon/make_icon.py" "$KEY"
  fi
done

# ---------------------------------------------------------------- deno (Seedbox: yt-dlp's YouTube JS challenges)
if builds seedbox && [[ -z "${SEEDBOX_DENO:-}" ]]; then
  BREW_DENO="$(command -v deno || true)"
  if [[ -n "$BREW_DENO" ]] && system_libs_only "$(realpath "$BREW_DENO")"; then
    export SEEDBOX_DENO="$(realpath "$BREW_DENO")"
    step "deno: using $SEEDBOX_DENO (system libraries only)"
  else
    if [[ -z "${DENO_VERSION:-}" ]]; then
      if [[ -n "$BREW_DENO" ]]; then
        DENO_VERSION="$("$BREW_DENO" --version | awk 'NR==1{print $2}')"
      else
        DENO_VERSION="$(curl -fsSL https://dl.deno.land/release-latest.txt | sed 's/^v//')"
      fi
    fi
    step "deno: official release v$DENO_VERSION (Homebrew's links Homebrew dylibs, which friends don't have)"
    DDIR="$CACHE/deno-$DENO_VERSION"
    if [[ ! -x "$DDIR/deno" ]]; then
      mkdir -p "$DDIR"
      BASE="https://github.com/denoland/deno/releases/download/v$DENO_VERSION/deno-aarch64-apple-darwin.zip"
      curl -fL --retry 3 -o "$DDIR/deno.zip" "$BASE"
      if curl -fsSL -o "$DDIR/deno.zip.sha256sum" "$BASE.sha256sum"; then
        WANT="$(grep -oE '[0-9a-f]{64}' "$DDIR/deno.zip.sha256sum" | head -1)"
        GOT="$(shasum -a 256 "$DDIR/deno.zip" | awk '{print $1}')"
        [[ "$WANT" == "$GOT" ]] || die "deno.zip checksum mismatch ($GOT != $WANT)"
      else
        echo "warning: no published checksum for deno v$DENO_VERSION; continuing" >&2
      fi
      unzip -oq "$DDIR/deno.zip" -d "$DDIR" && rm -f "$DDIR/deno.zip"
      chmod +x "$DDIR/deno"
    fi
    system_libs_only "$DDIR/deno" || die "downloaded deno links non-system libraries"
    export SEEDBOX_DENO="$DDIR/deno"
  fi
fi
builds seedbox && "$SEEDBOX_DENO" --version | head -1

MIN_OS="11.0"  # the newest any built app needs (the DMG's Read Me states it)
newer_os() { "$PYTHON" -c "import sys; a, b = (tuple(map(int, v.split('.'))) for v in sys.argv[1:]); print(sys.argv[1] if a > b else sys.argv[2])" "$1" "$2"; }

for KEY in $APPS; do
  NAME="$(app_name "$KEY")"
  # ---------------------------------------------------------------- PyInstaller
  step "$NAME: PyInstaller ($("$PYTHON" -c 'import PyInstaller; print(PyInstaller.__version__)'), $("$PYTHON" -V))"
  SEEDBOX_APP="$KEY" "$PYTHON" -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath "$WORK/dist" --workpath "$WORK/build-$KEY" "$HERE/seedbox.spec"
  APP="$OUT/$NAME.app"
  rm -rf "$APP"
  mv "$WORK/dist/$NAME.app" "$APP"
  rm -rf "${WORK:?}/dist/$NAME"  # the onedir COLLECT folder (already inside the .app)
  VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"

  step "$NAME: minimum macOS (every Mach-O in the bundle)"
  APP_MIN_OS="$("$PYTHON" "$HERE/minos.py" "$APP" | tail -1)"
  /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion $APP_MIN_OS" "$APP/Contents/Info.plist"
  echo "LSMinimumSystemVersion = $APP_MIN_OS"
  MIN_OS="$(newer_os "$APP_MIN_OS" "$MIN_OS")"

  # ---------------------------------------------------------------- codesign (ad-hoc)
  step "$NAME: codesign (ad-hoc)"
  xattr -cr "$APP"
  codesign --force --deep --sign - "$APP"
  codesign --verify --deep --strict "$APP"
  echo "signature ok"

  # ---------------------------------------------------------------- smoke test
  if [[ $SKIP_SMOKE == 0 ]]; then
    step "$NAME: smoke test (--server-only)"
    SMOKE="$WORK/smoke-$KEY"
    rm -rf "$SMOKE" && mkdir -p "$SMOKE"
    PORT="$("$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
    T0="$("$PYTHON" -c 'import time; print(time.time())')"
    env MUSICRECO_OUTPUT_ROOT="$SMOKE/crates" SEEDBOX_SUPPORT_DIR="$SMOKE/support" SEEDBOX_CACHE_DIR="$SMOKE/cache" \
        SEEDBOX_LOG_DIR="$SMOKE/logs" SEEDBOX_NO_UPDATE_CHECK=1 SEEDBOX_NO_MIGRATE=1 \
        "$APP/Contents/MacOS/$NAME" --server-only --port "$PORT" > "$SMOKE/server.out" 2>&1 &
    PID=$!
    trap 'kill $PID 2>/dev/null || true' EXIT
    OK=0
    for _ in $(seq 1 600); do
      if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then OK=1; break; fi
      kill -0 $PID 2>/dev/null || break
      sleep 0.05
    done
    T1="$("$PYTHON" -c 'import time; print(time.time())')"
    if [[ $OK != 1 ]]; then cat "$SMOKE/server.out"; die "$NAME did not answer /api/health"; fi
    printf 'healthy after %.2fs\n' "$("$PYTHON" -c "print($T1 - $T0)")"
    SYSTEM="$(curl -fs "http://127.0.0.1:$PORT/api/system")"
    echo "$SYSTEM"
    echo "$SYSTEM" | grep -q "\"app\":\"$KEY\"" || die "/api/system: not $NAME's server"
    echo "$SYSTEM" | grep -q "$NAME.app/Contents/Frameworks/bin/ffmpeg" || die "/api/system: ffmpeg not from the bundle"
    if [[ $KEY == seedbox ]]; then
      echo "$SYSTEM" | grep -q "$NAME.app/Contents/Frameworks/bin/deno" || die "/api/system: deno not from the bundle"
    fi
    curl -fs "http://127.0.0.1:$PORT/" | grep -qi "data-app=\"$KEY\"" || die "/ does not serve $NAME's page"
    kill $PID; wait $PID 2>/dev/null || true
    trap - EXIT
    echo "smoke test passed"
  fi
done

# ---------------------------------------------------------------- DMG
FIRST="$(app_name "${APPS%% *}")"
if [[ $SKIP_DMG == 0 ]]; then
  step "DMG"
  STAGE="$WORK/dmg"
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  for KEY in $APPS; do ditto "$OUT/$(app_name "$KEY").app" "$STAGE/$(app_name "$KEY").app"; done
  ln -s /Applications "$STAGE/Applications"
  sed "s/{{MIN_MACOS}}/$MIN_OS/g" "$HERE/README-friends.md" > "$STAGE/Read Me First.txt"
  DMG="$OUT/$FIRST-$VERSION-arm64.dmg"
  rm -f "$DMG"
  hdiutil create -quiet -volname "$FIRST $VERSION" -srcfolder "$STAGE" -fs HFS+ -format ULMO -ov "$DMG"
  hdiutil verify -quiet "$DMG"
  rm -rf "$STAGE"
fi

step "done in $(( $(date +%s) - BUILD_T0 ))s"
for KEY in $APPS; do
  APP="$OUT/$(app_name "$KEY").app"
  echo "app: $APP ($(du -sh "$APP" | awk '{print $1}'), macOS $(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' \
    "$APP/Contents/Info.plist")+, $(app_name "$KEY") $VERSION)"
done
[[ $SKIP_DMG == 1 ]] || echo "dmg: $DMG ($(du -h "$DMG" | awk '{print $1}'))"
