from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import MP3_BITRATE, safe_filename

log = logging.getLogger("musicdl.downloader")

# Domains that yt-dlp cannot handle natively for downloadable content
# (Spotify streams are DRM-protected; handled via spotdl instead).
SPOTIFY_HOSTS = ("open.spotify.com", "spotify.com")


@dataclass
class DownloadResult:
    filepath: Path
    title: str
    uploader: str | None
    source_url: str
    duration: float | None


def _base_opts(output_template: str) -> dict:
    return {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": MP3_BITRATE,
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "EmbedThumbnail",
                "already_have_thumbnail": False,
            },
        ],
        "writethumbnail": True,
        "quiet": False,
        "no_warnings": False,
        "noprogress": False,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "restrictfilenames": False,
        "windowsfilenames": True,
        # Extract IDs from URLs even for search terms.
        "default_search": "ytsearch",
        "extract_flat": False,
    }


def _pick_search_query(
    title: str | None,
    artist: str | None,
    variant: str | None,
) -> str:
    """Build a search query that steers yt-dlp toward the right remix/edit."""
    parts: list[str] = []
    if artist:
        parts.append(artist)
    if title:
        parts.append(title)
    if variant:
        parts.append(variant)
    if not parts:
        raise ValueError("Need at least one of: title, artist, variant")
    return " ".join(parts)


def _finalize_filename(reported_path: str) -> Path:
    """yt-dlp may report .webm/.m4a before postprocess; convert to .mp3."""
    p = Path(reported_path)
    mp3 = p.with_suffix(".mp3")
    return mp3 if mp3.exists() else p


def download_url(
    url: str,
    dest_dir: Path,
    filename_hint: str | None = None,
    playlist_index: int | None = None,
) -> DownloadResult:
    """Download a single track URL as 320 kbps MP3 into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if filename_hint:
        base = safe_filename(filename_hint)
        if playlist_index is not None:
            base = f"{playlist_index:02d} - {base}"
        outtmpl = str(dest_dir / f"{base}.%(ext)s")
    else:
        # Fall back to the track's own title.
        prefix = f"{playlist_index:02d} - " if playlist_index is not None else ""
        outtmpl = str(dest_dir / f"{prefix}%(title)s.%(ext)s")

    opts = _base_opts(outtmpl)
    # Don't accidentally traverse playlists when caller passed a single item.
    opts["noplaylist"] = True

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DownloadError(f"No info returned for {url}")
        reported = ydl.prepare_filename(info)
        filepath = _finalize_filename(reported)
        return DownloadResult(
            filepath=filepath,
            title=info.get("title") or filepath.stem,
            uploader=info.get("uploader"),
            source_url=info.get("webpage_url") or url,
            duration=info.get("duration"),
        )


def download_search(
    title: str | None,
    artist: str | None = None,
    variant: str | None = None,
    *,
    dest_dir: Path,
    playlist_index: int | None = None,
    filename_hint: str | None = None,
) -> DownloadResult:
    """Search YouTube for a track and download the top match at 320 kbps."""
    query = _pick_search_query(title, artist, variant)
    # yt-dlp handles 'ytsearch1:...' natively; we ask for exactly one result.
    return download_url(
        f"ytsearch1:{query}",
        dest_dir=dest_dir,
        filename_hint=filename_hint or query,
        playlist_index=playlist_index,
    )


def extract_playlist_entries(url: str) -> tuple[str, list[dict]]:
    """Return (playlist_title, [entry_info,...]) without downloading."""
    opts = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noprogress": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            return ("playlist", [])
        entries = list(info.get("entries") or [])
        title = info.get("title") or info.get("id") or "playlist"
        return (title, entries)


def download_playlist_entries(
    entries: Iterable[dict],
    dest_dir: Path,
) -> list[DownloadResult]:
    """Download each entry into dest_dir, numbered by playlist order."""
    results: list[DownloadResult] = []
    for idx, entry in enumerate(entries, start=1):
        if not entry:
            continue
        url = entry.get("url") or entry.get("webpage_url")
        title = entry.get("title")
        if not url:
            log.warning("Skipping entry #%d — no URL: %r", idx, entry)
            continue
        try:
            result = download_url(
                url,
                dest_dir=dest_dir,
                filename_hint=title,
                playlist_index=idx,
            )
            results.append(result)
            log.info("[%d] %s -> %s", idx, title or url, result.filepath)
        except DownloadError as e:
            log.error("[%d] failed %s: %s", idx, title or url, e)
    return results


def get_video_metadata(url: str) -> dict:
    """Return raw info dict for a URL (single video, no download)."""
    opts = {"quiet": True, "skip_download": True, "noprogress": True, "noplaylist": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info or {}


def is_spotify_url(url: str) -> bool:
    lower = url.lower()
    return any(host in lower for host in SPOTIFY_HOSTS)
