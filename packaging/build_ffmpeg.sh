#!/usr/bin/env bash
# Build an LGPL ffmpeg for Seedbox 2 from pinned sources (no one publishes an LGPL build for macOS).
#
#   packaging/build_ffmpeg.sh OUT_DIR     writes OUT_DIR/ffmpeg; a second run reuses it
#
# The configuration stays LGPL: no --enable-gpl, no --enable-nonfree, and of the external libraries only libopus (BSD),
# which the stems' Opus files need (seedbox2/analysis/stems.py), and LAME (LGPL), the only MP3 encoder ffmpeg has, which
# every download needs (musicreco/youtube.py writes MP3). The script refuses a result that says otherwise.
# Needs a C compiler, make and pkg-config, and nasm on x86_64 (without it the build works but decodes more slowly).
# MACOSX_DEPLOYMENT_TARGET sets the oldest macOS it runs on. Also runs on Linux, for checking the script.
set -euo pipefail

FFMPEG_VERSION=8.1.3
FFMPEG_SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3
OPUS_VERSION=1.5.2
OPUS_SHA256=65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1
LAME_VERSION=3.100
LAME_SHA256=ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e

OUT="$(mkdir -p "${1:?usage: build_ffmpeg.sh OUT_DIR}" && cd "$1" && pwd)"
# Reuse an earlier build only if it has every encoder this script adds (one from before LAME doesn't).
if [[ -x "$OUT/ffmpeg" ]] && [[ "$("$OUT/ffmpeg" -hide_banner -encoders | grep -cE ' (libopus|libmp3lame) ')" == 2 ]]; then
  echo "ffmpeg: reusing $OUT/ffmpeg"; exit 0
fi
SRC="$OUT/src"
PREFIX="$OUT/prefix"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
mkdir -p "$SRC" "$PREFIX"

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
fetch() {  # fetch URL SHA256 FILE
  [[ -f "$3" ]] || curl -fsSL --retry 3 -o "$3" "$1"
  [[ "$(sha256 "$3")" == "$2" ]] || { echo "error: wrong checksum for $1" >&2; rm -f "$3"; exit 1; }
}

fetch "https://downloads.xiph.org/releases/opus/opus-$OPUS_VERSION.tar.gz" "$OPUS_SHA256" "$SRC/opus.tar.gz"
fetch "https://downloads.sourceforge.net/project/lame/lame/$LAME_VERSION/lame-$LAME_VERSION.tar.gz" "$LAME_SHA256" \
  "$SRC/lame.tar.gz"
fetch "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz" "$FFMPEG_SHA256" "$SRC/ffmpeg.tar.xz"

rm -rf "$SRC/opus-$OPUS_VERSION" "$SRC/lame-$LAME_VERSION" "$SRC/ffmpeg-$FFMPEG_VERSION"
tar -xzf "$SRC/opus.tar.gz" -C "$SRC"
tar -xzf "$SRC/lame.tar.gz" -C "$SRC"
tar -xJf "$SRC/ffmpeg.tar.xz" -C "$SRC"

(cd "$SRC/opus-$OPUS_VERSION"
 ./configure --quiet --prefix="$PREFIX" --libdir="$PREFIX/lib" --disable-shared --enable-static --disable-doc \
   --disable-extra-programs
 command make -j"$JOBS" install >/dev/null)

# LAME 3.100's export list names a symbol the library no longer has, which macOS's linker refuses.
sed -i.orig '/lame_init_old/d' "$SRC/lame-$LAME_VERSION/include/libmp3lame.sym"
(cd "$SRC/lame-$LAME_VERSION"
 ./configure --quiet --prefix="$PREFIX" --libdir="$PREFIX/lib" --disable-shared --enable-static --disable-frontend \
   --disable-decoder --disable-gtktest
 command make -j"$JOBS" install >/dev/null)

EXTRA=()
if [[ "$(uname -s)" == "Darwin" ]]; then
  EXTRA+=(--enable-audiotoolbox --enable-securetransport)  # system frameworks: Apple's AAC encoder, https inputs
fi
if [[ "$(uname -m)" == "x86_64" ]] && ! command -v nasm >/dev/null; then
  echo "warning: no nasm: building without x86 assembly, which decodes more slowly" >&2
  EXTRA+=(--disable-x86asm)
fi

(cd "$SRC/ffmpeg-$FFMPEG_VERSION"
 PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" ./configure --prefix="$PREFIX" --pkg-config-flags=--static \
   --extra-cflags="-I$PREFIX/include" --extra-ldflags="-L$PREFIX/lib" \
   --disable-autodetect --disable-doc --disable-debug --disable-ffplay --disable-ffprobe --disable-avdevice \
   --enable-zlib --enable-libopus --enable-libmp3lame ${EXTRA[@]+"${EXTRA[@]}"} >/dev/null
 command make -j"$JOBS" >/dev/null
 cp ffmpeg "$OUT/ffmpeg.new")
strip "$OUT/ffmpeg.new"

CONFIGURATION="$("$OUT/ffmpeg.new" -hide_banner -version)"
ENCODERS="$("$OUT/ffmpeg.new" -hide_banner -encoders)"
if grep -E -- '--enable-(gpl|nonfree|version3)' <<<"$CONFIGURATION"; then
  echo "error: this ffmpeg isn't plain LGPL 2.1" >&2; exit 1
fi
grep -q ' libopus ' <<<"$ENCODERS" || { echo "error: ffmpeg has no libopus" >&2; exit 1; }
grep -q ' libmp3lame ' <<<"$ENCODERS" || { echo "error: ffmpeg has no libmp3lame" >&2; exit 1; }
mv "$OUT/ffmpeg.new" "$OUT/ffmpeg"
rm -rf "$SRC" "$PREFIX"
du -h "$OUT/ffmpeg"
