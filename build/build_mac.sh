#!/usr/bin/env bash
# Build the musicdl.app bundle for macOS.
# Downloads a static ffmpeg for the current architecture (arm64 vs x86_64),
# stages it, then runs PyInstaller. Output: dist/musicdl.app
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "→ creating venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ installing build deps"
pip install -q -e . pyinstaller

VENDOR=build/vendor
mkdir -p "$VENDOR"

if [ ! -x "$VENDOR/ffmpeg" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    arm64|aarch64)
      URL="https://www.osxexperts.net/ffmpeg71arm.zip"
      ;;
    x86_64)
      URL="https://evermeet.cx/ffmpeg/getrelease/zip"
      ;;
    *)
      echo "unsupported arch: $ARCH" >&2
      exit 1
      ;;
  esac
  echo "→ downloading ffmpeg for $ARCH"
  TMPZIP="$(mktemp -t ffmpeg-XXXXXX).zip"
  curl -fL "$URL" -o "$TMPZIP"
  unzip -oq "$TMPZIP" -d "$VENDOR"
  rm -f "$TMPZIP"
  chmod +x "$VENDOR/ffmpeg"
fi

echo "→ ffmpeg staged: $(file "$VENDOR/ffmpeg" | head -1)"
echo "→ running pyinstaller"
rm -rf build/musicdl build/musicdl.app dist/musicdl.app dist/musicdl
pyinstaller --clean --noconfirm build/musicdl.spec

echo
echo "✓ built: dist/musicdl.app"
echo "  Move it to /Applications and double-click. On first launch macOS may"
echo "  block the unsigned app — right-click → Open → Open Anyway."
