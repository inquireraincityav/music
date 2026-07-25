"""Spotify support via spotdl (invoked as a subprocess so we stay decoupled)."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .config import MP3_BITRATE

log = logging.getLogger("musicdl.spotify")


def download_spotify(url: str, dest_dir: Path) -> int:
    """Run spotdl for the given Spotify URL (track/album/playlist).

    spotdl handles playlists by writing multiple files into the current dir,
    which is why we `cd` into dest_dir via subprocess `cwd=`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "spotdl",
        "download",
        url,
        "--bitrate",
        f"{MP3_BITRATE}k",
        "--format",
        "mp3",
        # {list-position} is empty for singles, non-empty for playlists.
        "--output",
        "{list-position} - {artist} - {title}.{output-ext}",
        "--threads",
        "4",
    ]
    log.info("spotdl: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=dest_dir)
    return proc.returncode
