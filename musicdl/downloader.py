from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

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

# When the requested track name has a version qualifier in parens/brackets and
# that text contains one of these keywords, the resulting search hit's title
# must also contain the qualifier — otherwise we skip it. Prevents "downloaded
# the original instead of the (VIP Mix)" surprises.
_VERSION_KEYWORDS = re.compile(
    r"\b(?:remix|mix|edit|bootleg|mashup|version|rework|rmx|vip|dub|"
    r"extended|club|radio|instrumental|acapella|flip|refix)\b",
    re.IGNORECASE,
)
_PAREN_CONTENT = re.compile(r"[\(\[]([^)\]]+)[\)\]]")


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


def _extract_version_hint(query: str) -> Optional[str]:
    """Return contents of the first (…) / […] block that includes a version
    keyword. Used to require that our download's title contains the same
    qualifier (so we don't get 'Original Mix' when 'VIP Mix' was requested).
    """
    for m in _PAREN_CONTENT.finditer(query):
        content = m.group(1)
        if _VERSION_KEYWORDS.search(content):
            return content.strip()
    return None


def _normalize_for_match(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _title_has_version(title: str, hint: str) -> bool:
    if not hint:
        return True
    return _normalize_for_match(hint) in _normalize_for_match(title)


def _search_candidates(
    query: str,
    n: int = _SEARCH_MAX_RESULTS,
    engine: str = "ytsearch",
) -> list[dict]:
    """Fetch top-N search result metadata (no download) via a yt-dlp engine.

    engine="ytsearch" for YouTube, "scsearch" for SoundCloud.
    """
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noprogress": True,
        "default_search": engine,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{engine}{n}:{query}", download=False)
    return [e for e in (info.get("entries") or []) if e]


def _pick_candidate(
    candidates: list[dict],
    max_dur: int = _SEARCH_MAX_DURATION_SEC,
    version_hint: Optional[str] = None,
) -> tuple[dict | None, list[str]]:
    """Return (winning candidate, list of rejection reasons)."""
    reasons: list[str] = []
    for c in candidates:
        title = c.get("title") or c.get("id") or "?"
        dur = c.get("duration")
        if dur is not None and dur > max_dur:
            reasons.append(f"'{title}' too long ({int(dur)}s)")
            continue
        if version_hint and not _title_has_version(title, version_hint):
            reasons.append(f"'{title}' missing version '{version_hint}'")
            continue
        return c, reasons
    return None, reasons


def _search_and_download(
    engine: str,
    query: str,
    version_hint: Optional[str],
    dest_dir: Path,
    filename_hint: Optional[str],
    playlist_index: Optional[int],
) -> "DownloadResult":
    candidates = _search_candidates(query, engine=engine)
    label = "YouTube" if engine == "ytsearch" else "SoundCloud"
    if not candidates:
        raise DownloadError(f"no {label} results for: {query}")
    winner, reasons = _pick_candidate(candidates, version_hint=version_hint)
    if not winner:
        detail = "; ".join(reasons) if reasons else "no candidates"
        raise DownloadError(f"no suitable {label} result for: {query} ({detail})")
    url = winner.get("url") or winner.get("webpage_url")
    if not url:
        raise DownloadError(f"{label} hit has no URL for: {query}")
    log.info("%s '%s' -> %s", engine, query, winner.get("title") or url)
    return download_url(
        url,
        dest_dir=dest_dir,
        filename_hint=filename_hint or query,
        playlist_index=playlist_index,
    )


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
    """Search YouTube then SoundCloud for a plausible-length matching result.

    If the query contains a version qualifier in parens (e.g. "(VIP Mix)",
    "(Prospa Remix)", "(Extended Mix)"), only results whose title also
    contains that qualifier are accepted — the "original mix" is not a valid
    substitute. Errors distinguish "nothing found at all", "only wrong
    version found", and "everything found was too long" so the bot can give
    the user a specific hint.
    """
    query = _pick_search_query(title, artist, variant)
    version_hint = _extract_version_hint(query)

    try:
        return _search_and_download(
            "ytsearch", query, version_hint, dest_dir, filename_hint, playlist_index
        )
    except DownloadError as yt_err:
        log.info("YouTube search failed for '%s': %s", query, yt_err)
        yt_msg = str(yt_err)

    try:
        return _search_and_download(
            "scsearch", query, version_hint, dest_dir, filename_hint, playlist_index
        )
    except DownloadError as sc_err:
        sc_msg = str(sc_err)
        if version_hint:
            raise DownloadError(
                f"Requested version '{version_hint}' not found on YouTube or "
                f"SoundCloud for: {query}\n  YT: {yt_msg}\n  SC: {sc_msg}"
            )
        if "too long" in yt_msg and "too long" in sc_msg:
            raise DownloadError(
                f"All results on YouTube and SoundCloud were too long "
                f"(>{_SEARCH_MAX_DURATION_SEC // 60} min) for: {query}\n"
                f"  YT: {yt_msg}\n  SC: {sc_msg}"
            )
        raise DownloadError(
            f"No usable result on YouTube or SoundCloud for: {query}\n"
            f"  YT: {yt_msg}\n  SC: {sc_msg}"
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
