"""Per-app audio tap for Spotify (DEFERRED — stub only).

This is the Audio-Downloader-style path: when spotdl can't find or returns
the wrong version of a Spotify-exclusive track, record the real audio playing
from the Spotify desktop app via BlackHole + ScreenCaptureKit.

Not implemented yet because it:
  - Only works on macOS 14.4+ (ScreenCaptureKit per-app audio)
  - Requires the BlackHole 2ch kernel driver installed
  - Needs Screen Recording + Automation(Spotify) permissions
  - Takes real time (4-minute song = 4 minutes)
  - Needs Swift binaries we don't ship

The hook point: `is_spotify_tap_available()` + `record_spotify_track(url, out)`.
When we build this, bot.py's spotify path can check availability and fall
back to the tap when spotdl's result fails a quality or version check.

For now, calling record_spotify_track raises NotImplementedError.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_spotify_tap_available() -> bool:
    """True once the Spotify audio tap is wired in (currently always False)."""
    return False


def record_spotify_track(spotify_url: str, out_dir: Path) -> Path:
    """Record one Spotify track via per-app audio tap. Not implemented."""
    if sys.platform != "darwin":
        raise NotImplementedError(
            "Spotify audio tap is a Mac-only feature. Use spotdl instead."
        )
    raise NotImplementedError(
        "Spotify audio tap is deferred — see musicdl/spotify_tap.py. "
        "Use spotdl (default) for Spotify URLs until this lands."
    )
