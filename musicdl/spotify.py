"""Spotify support via spotdl (invoked as a subprocess so we stay decoupled)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import MP3_BITRATE

log = logging.getLogger("musicdl.spotify")


def download_spotify(url: str, dest_dir: Path) -> int:
    """Run spotdl for the given Spotify URL (track/album/playlist).

    spotdl handles playlists by writing multiple files into the current dir,
    which is why we `cd` into dest_dir via subprocess `cwd=`.

    Config choices worth calling out:

    ``--audio youtube-music youtube``
        Search YouTube Music FIRST, YouTube as fallback. YouTube Music has
        canonical artist-verified uploads with clean metadata, so it avoids
        the "picked a lyric video / live version / rip channel" failure
        mode that plain YouTube search hits. Multi-value flag — spotdl
        iterates providers in the given order.

    ``--search-query "{artist} - {title} - {album}"``
        Steer the match toward the album version. The default query is
        just artist + title, which can match a compilation or live album
        cut. Including the album name helps disambiguate remasters, EPs,
        deluxe editions, etc.

    ``--restrict none``
        Preserve Unicode characters (accents, symbols) in filenames.
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
        "--audio",
        "youtube-music",
        "youtube",
        "--search-query",
        "{artist} - {title} - {album}",
        # {list-position} is empty for singles, non-empty for playlists.
        "--output",
        "{list-position} - {artist} - {title}.{output-ext}",
        "--threads",
        "4",
        "--print-errors",
    ]
    # Cookies for authenticated services propagate via env var, honored by
    # the yt-dlp process spotdl spawns internally.
    env = os.environ.copy()
    cookies = env.get("MUSICDL_COOKIES_FILE")
    if cookies and Path(cookies).exists():
        # spotdl forwards --cookie-file to yt-dlp under the hood.
        cmd.extend(["--cookie-file", cookies])

    log.info("spotdl: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=dest_dir, env=env)
    return proc.returncode
