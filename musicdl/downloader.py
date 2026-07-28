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

# Search behavior — reject overly long results (usually full DJ sets or radio
# shows) and try up to N candidates before giving up.
_SEARCH_MAX_DURATION_SEC = 720  # 12 minutes
_SEARCH_MAX_RESULTS = 5


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


def _verify_and_finalize(reported_path: str) -> Path:
    """Confirm a real .mp3 landed on disk; clean up stragglers and raise if not."""
    p = Path(reported_path)
    mp3 = p.with_suffix(".mp3")
    if mp3.exists() and mp3.stat().st_size > 0:
        _cleanup_partials(mp3)
        return mp3
    _cleanup_partials(mp3)
    raise DownloadError(
        f"Audio extraction produced no .mp3 (source may be image-only, "
        f"age-restricted, or DRM-protected): {p.name}"
    )


def _cleanup_partials(target_mp3: Path) -> None:
    """Remove leftover .webp/.part/.jpg/.m4a etc. sitting next to target_mp3."""
    stem = target_mp3.stem
    for f in target_mp3.parent.glob(f"{stem}.*"):
        if f.suffix.lower() == ".mp3":
            continue
        try:
            f.unlink()
        except OSError:
            pass


def _search_candidates(query: str, n: int = _SEARCH_MAX_RESULTS) -> list[dict]:
    """Fetch top-N YouTube search result metadata (no download)."""
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noprogress": True,
        "default_search": "ytsearch",
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info.get("entries") or []) if e]


def _pick_candidate(
    candidates: list[dict],
    max_dur: int = _SEARCH_MAX_DURATION_SEC,
) -> tuple[dict | None, list[str]]:
    """Return (winning candidate, list of rejection reasons)."""
    reasons: list[str] = []
    for c in candidates:
        title = c.get("title") or c.get("id") or "?"
        dur = c.get("duration")
        if dur is not None and dur > max_dur:
            reasons.append(f"'{title}' too long ({int(dur)}s)")
            continue
        return c, reasons
    return None, reasons


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
        filepath = _verify_and_finalize(reported)
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
    """Search YouTube, filter to a plausible-length result, download it as MP3.

    Iterates the top ``_SEARCH_MAX_RESULTS`` candidates and picks the first one
    at or under ``_SEARCH_MAX_DURATION_SEC``. Raises DownloadError with the
    rejection reasons if nothing passes — much better than silently returning
    a random 90-minute DJ set that happens to feature the track.
    """
    query = _pick_search_query(title, artist, variant)
    candidates = _search_candidates(query)
    if not candidates:
        raise DownloadError(f"No search results for: {query}")

    winner, reasons = _pick_candidate(candidates)
    if not winner:
        detail = "\n  ".join(reasons) if reasons else "no candidates"
        raise DownloadError(
            f"No result under {_SEARCH_MAX_DURATION_SEC // 60} min for: "
            f"{query}\n  {detail}"
        )

    url = winner.get("url") or winner.get("webpage_url")
    if not url:
        raise DownloadError(f"Search hit has no URL for: {query}")

    log.info("search '%s' -> %s", query, winner.get("title") or url)
    return download_url(
        url,
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
