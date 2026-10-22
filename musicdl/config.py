from __future__ import annotations

import os
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("MUSICDL_OUTPUT_DIR", Path.home() / "Desktop" / "MusicDownloads")
)

MP3_BITRATE = "320"
MP3_EXT = "mp3"

# Reserved words / filesystem-hostile characters to strip from filenames.
_FS_BAD = '<>:"/\\|?*\x00'


def safe_filename(name: str, max_len: int = 180) -> str:
    """Return a filesystem-safe filename component."""
    cleaned = "".join(("_" if c in _FS_BAD else c) for c in name).strip(" .")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_len]
