from __future__ import annotations

import glob as _glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
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
# shows) and try up to N candidates before giving up. We fetch a wider pool
# than we need so the audio-vs-video preference ranking has something to pick
# from; the extras don't cost much since this is metadata-only.
_SEARCH_MAX_DURATION_SEC = 720  # 12 minutes
_SEARCH_MAX_RESULTS = 8

# When the requested track name has a version qualifier in parens/brackets and
# that text contains one of these keywords, the resulting search hit's title
# must also contain the qualifier — otherwise we skip it. Prevents "downloaded
# the original instead of the (VIP Mix)" surprises.
_VERSION_KEYWORDS = re.compile(
    r"\b(?:remix|mix|edit|bootleg|mashup|version|rework|rmx|vip|dub|"
    r"extended|club|radio|instrumental|acapella|flip|refix|tweak|"
    r"dirty|clean|short|long|intro|outro|drum|chop|chopped|screwed|"
    r"slowed|sped\s*up|mashup|blend)\b",
    re.IGNORECASE,
)
_PAREN_CONTENT = re.compile(r"[\(\[]([^)\]]+)[\)\]]")


def _bundled_ffmpeg_dir() -> Optional[str]:
    """Return the directory containing a bundled ffmpeg, if we're running in
    a PyInstaller bundle that includes one; else None so yt-dlp falls back
    to PATH resolution.
    """
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidate = Path(meipass) / exe
    if candidate.is_file():
        return str(candidate.parent)
    return None


def _ffprobe_cmd() -> Optional[str]:
    """Locate ffprobe (prefer the bundled copy sitting next to ffmpeg)."""
    ff_dir = _bundled_ffmpeg_dir()
    if ff_dir:
        exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        candidate = Path(ff_dir) / exe
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffprobe")


def probe_duration(path: Path) -> Optional[float]:
    """Return a file's duration in seconds via ffprobe, or None if it can't tell."""
    probe = _ffprobe_cmd()
    if not probe:
        return None
    try:
        out = subprocess.run(
            [
                probe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur else None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError, OSError):
        return None


# Tolerance for "did we download the whole thing?" checks. 5 seconds + 3% of
# the expected duration — wider for long files, strict for short ones.
_DURATION_ABS_TOL = 5.0
_DURATION_REL_TOL = 0.03


def _durations_match(actual: Optional[float], expected: Optional[float]) -> bool:
    """True if actual duration is within tolerance of expected."""
    if not actual or not expected:
        return False
    tol = max(_DURATION_ABS_TOL, expected * _DURATION_REL_TOL)
    return abs(actual - expected) <= tol


@dataclass
class DownloadResult:
    filepath: Path
    title: str
    uploader: str | None
    source_url: str
    duration: float | None


def _base_opts(output_template: str) -> dict:
    opts: dict = {
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
    # Optional cookies file for authenticated services (BPMSupreme, DJcity,
    # age-restricted YouTube, etc.). Point MUSICDL_COOKIES_FILE at a
    # Netscape-format cookies.txt exported from your logged-in browser.
    cookies_path = os.environ.get("MUSICDL_COOKIES_FILE")
    if cookies_path and Path(cookies_path).is_file():
        opts["cookiefile"] = cookies_path
    # PyInstaller bundle: use the ffmpeg we shipped inside the app.
    ff_dir = _bundled_ffmpeg_dir()
    if ff_dir:
        opts["ffmpeg_location"] = ff_dir
    return opts


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


def _preferred_stem_from_info(info: dict) -> Optional[str]:
    """Build a 'Title - Artist' stem from yt-dlp metadata when both parts exist.

    Prefers the track/artist fields that yt-dlp pulls out of music videos'
    descriptions; falls back to None (caller keeps whatever filename yt-dlp
    chose) when either side is missing.
    """
    title = (info.get("track") or info.get("title") or "").strip()
    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or ""
    ).strip()
    # Drop channel-style suffixes like " - Topic" that YT Music auto-appends.
    if artist.endswith(" - Topic"):
        artist = artist[: -len(" - Topic")].strip()
    if not title or not artist:
        return None
    # Don't duplicate the artist if the title already contains it.
    if artist.lower() in title.lower():
        return title
    return f"{title} - {artist}"


def _rename_to_preferred(
    mp3: Path,
    info: dict,
    playlist_index: Optional[int],
) -> Path:
    """Rename an untagged URL download to 'Title - Artist.mp3' when possible."""
    stem = _preferred_stem_from_info(info)
    if not stem:
        return mp3
    base = safe_filename(stem)
    if playlist_index is not None:
        base = f"{playlist_index:02d} - {base}"
    target = mp3.with_name(f"{base}.mp3")
    if target == mp3 or target.exists():
        return mp3
    try:
        mp3.rename(target)
    except OSError:
        return mp3
    return target


def _enforce_title_artist_order(
    mp3: Path,
    info: dict,
    playlist_index: Optional[int],
) -> Path:
    """If the current filename is 'Artist - Title' but metadata says it should
    be 'Title - Artist', flip it. Used as a last-line defense against any
    code path that still hands us a hint in the old order.

    No-op unless yt-dlp's metadata provides BOTH `track` (song title) and
    `artist` so we can confidently decide which part is which.
    """
    track = (info.get("track") or "").strip()
    artist_raw = (info.get("artist") or "").strip()
    if not track or not artist_raw:
        return mp3  # nothing to compare against — leave the hint intact

    # Strip the "NN - " playlist prefix from the current stem for comparison.
    stem = mp3.stem
    prefix = ""
    m = re.match(r"^(\d{2,3}\s*[-\.]\s*)(.+)$", stem)
    if m:
        prefix = m.group(1)
        core = m.group(2)
    else:
        core = stem

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower()).strip()

    # Candidate "Title - Artist" and "Artist - Title" strings per metadata.
    want = f"{track} - {artist_raw}"
    reversed_form = f"{artist_raw} - {track}"

    if norm(core) == norm(want):
        return mp3  # already correct
    if norm(core) == norm(reversed_form):
        # Confidently in the wrong order — flip it.
        new_core = safe_filename(want)
        target = mp3.with_name(f"{prefix}{new_core}.mp3")
        if target == mp3 or target.exists():
            return mp3
        try:
            mp3.rename(target)
            log.info("flipped filename order: %r -> %r", mp3.name, target.name)
            return target
        except OSError:
            return mp3
    # Hint didn't match either canonical form (e.g. an extended qualifier the
    # metadata doesn't carry) — leave it alone.
    return mp3


def _cleanup_partials(target_mp3: Path) -> None:
    """Remove leftover .webp/.part/.jpg/.m4a etc. sitting next to target_mp3.

    Uses glob.escape so stems containing brackets (e.g. "Song [Extended Mix]")
    or other glob-special chars still match the actual sibling files.
    """
    stem = target_mp3.stem
    pattern = _glob.escape(stem) + ".*"
    for f in target_mp3.parent.glob(pattern):
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


def _probe_opts(default_search: str = "ytsearch") -> dict:
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noprogress": True,
        "default_search": default_search,
    }
    cookies_path = os.environ.get("MUSICDL_COOKIES_FILE")
    if cookies_path and Path(cookies_path).is_file():
        opts["cookiefile"] = cookies_path
    return opts


def _search_candidates(
    query: str,
    n: int = _SEARCH_MAX_RESULTS,
    engine: str = "ytsearch",
) -> list[dict]:
    """Fetch top-N search result metadata (no download) via a yt-dlp engine.

    engine="ytsearch" for YouTube, "scsearch" for SoundCloud.
    """
    with YoutubeDL(_probe_opts(default_search=engine)) as ydl:
        info = ydl.extract_info(f"{engine}{n}:{query}", download=False)
    return [e for e in (info.get("entries") or []) if e]


_AUDIO_HINTS = re.compile(
    r"\b(?:official\s+audio|audio\s+only|hq\s+audio|full\s+audio|just\s+audio)\b",
    re.IGNORECASE,
)
# "Visualizer" uploads are audio with a stock loop — treat as audio-tier too.
_AUDIO_TIER_EXTRAS = re.compile(r"\b(?:visualizer|visualiser|audio)\b", re.IGNORECASE)
_VIDEO_HINTS = re.compile(
    r"\b(?:official\s+(?:music\s+)?video|music\s+video|m/?v|"
    r"official\s+mv|lyric\s+video|lyrics\s+video|live\s+performance|"
    r"behind\s+the\s+scenes|dance\s+video|choreography)\b",
    re.IGNORECASE,
)


def _preference_tier(candidate: dict) -> int:
    """Rank a candidate 0 (best) → 3 (worst). Lower wins.

    0 — Explicit audio upload ("Official Audio", "- Topic" uploader).
    1 — Audio-ish (bare "Audio" or "Visualizer" in title).
    2 — Neutral (no audio/video marker in title).
    3 — Video upload ("Official Video", "Music Video", "Lyric Video").

    "- Topic" channels are YouTube Music's auto-generated artist channels
    and are always audio-only; they beat even an explicit "Official Audio"
    upload because they're the actual master.
    """
    title = candidate.get("title") or ""
    uploader = candidate.get("uploader") or candidate.get("channel") or ""
    if uploader.strip().endswith("- Topic"):
        return 0
    if _AUDIO_HINTS.search(title):
        return 0
    if _VIDEO_HINTS.search(title):
        return 3
    if _AUDIO_TIER_EXTRAS.search(title):
        return 1
    return 2


def _pick_candidate(
    candidates: list[dict],
    max_dur: int = _SEARCH_MAX_DURATION_SEC,
    version_hint: Optional[str] = None,
) -> tuple[dict | None, list[str]]:
    """Return (winning candidate, list of rejection reasons).

    First rejects anything that fails the duration + version-hint filter,
    then returns the best remaining candidate by audio-vs-video preference
    tier (audio uploads > neutral > video uploads). Ties within a tier are
    broken by the search engine's own ordering (first-returned wins).
    """
    reasons: list[str] = []
    survivors: list[dict] = []
    for c in candidates:
        title = c.get("title") or c.get("id") or "?"
        dur = c.get("duration")
        if dur is not None and dur > max_dur:
            reasons.append(f"'{title}' too long ({int(dur)}s)")
            continue
        if version_hint and not _title_has_version(title, version_hint):
            reasons.append(f"'{title}' missing version '{version_hint}'")
            continue
        survivors.append(c)
    if not survivors:
        return None, reasons
    survivors.sort(key=_preference_tier)
    winner = survivors[0]
    picked_tier = _preference_tier(winner)
    if picked_tier > 0 and len(survivors) > 1:
        # Log when we had to settle for a non-audio result, so the user can
        # see WHY their search returned the "video" upload (no audio one existed).
        log.info(
            "no audio upload available; best remaining tier=%d title=%r",
            picked_tier,
            winner.get("title"),
        )
    return winner, reasons


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


def _parse_mirror_dirs() -> list[Path]:
    """Mirror roots to copy every finished MP3 into (iCloud, external drive…)."""
    raw = os.environ.get("MUSICDL_MIRROR_DIRS", "").strip()
    if not raw:
        return []
    out: list[Path] = []
    for part in raw.split(os.pathsep if os.pathsep in raw else ","):
        s = part.strip()
        if not s:
            continue
        p = Path(s).expanduser()
        out.append(p)
    return out


def _mirror_copy(filepath: Path, primary_root: Path) -> None:
    """Copy filepath into every configured mirror root, preserving the
    sub-path under primary_root (Singles/, Playlists/<name>/, Sets/<name>/)."""
    mirrors = _parse_mirror_dirs()
    if not mirrors:
        return
    try:
        rel = filepath.resolve().relative_to(primary_root.resolve())
    except ValueError:
        # File isn't under the primary root — just mirror the filename.
        rel = Path(filepath.name)
    for mroot in mirrors:
        target = mroot / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size == filepath.stat().st_size:
                continue  # already mirrored
            shutil.copy2(filepath, target)
            log.info("mirrored -> %s", target)
        except OSError as e:
            log.warning("mirror copy failed for %s -> %s: %s", filepath, target, e)


def _primary_root() -> Path:
    """The configured download root (used to compute mirror sub-paths)."""
    return Path(
        os.environ.get("MUSICDL_OUTPUT_DIR") or (Path.home() / "Desktop" / "MusicDownloads")
    )


def _probe_expected_duration(url: str) -> Optional[float]:
    """Fetch a URL's expected duration without downloading (metadata only)."""
    try:
        with YoutubeDL(_probe_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        dur = info.get("duration")
        return float(dur) if dur else None
    except Exception:  # noqa: BLE001
        return None


def _existing_match(target_mp3: Path, expected_dur: Optional[float]) -> Optional[Path]:
    """If target_mp3 already exists AND its duration matches expected, return it.

    Lets re-sent URLs skip in-place instead of re-downloading. Falls back to a
    bare size check when ffprobe isn't available or we don't know the
    expected duration.
    """
    if not target_mp3.exists() or target_mp3.stat().st_size == 0:
        return None
    if expected_dur:
        actual = probe_duration(target_mp3)
        if actual is not None and _durations_match(actual, expected_dur):
            return target_mp3
        # Known mismatch — pretend it's not there so the re-download overwrites.
        return None
    # Unknown expected duration: trust the file if it's non-trivially sized.
    if target_mp3.stat().st_size > 50_000:
        return target_mp3
    return None


def download_url(
    url: str,
    dest_dir: Path,
    filename_hint: str | None = None,
    playlist_index: int | None = None,
) -> DownloadResult:
    """Download a single track URL as 320 kbps MP3 into dest_dir.

    Skip-if-already-downloaded: when the target file already exists and its
    duration matches what the URL claims, we return the existing file instead
    of re-downloading. This makes re-sending a URL (or re-running a half-
    finished set) idempotent.

    Duration verification: after a fresh download we probe the mp3 and compare
    against yt-dlp's reported duration. A silent truncation (ffmpeg wrote 30s
    of a 4-minute song) raises instead of returning quietly.

    Mirror copy: after a successful download the file is copied into every
    directory listed in MUSICDL_MIRROR_DIRS (preserving the Singles/Playlists/
    Sets sub-path) so a second machine (via iCloud, Dropbox, SMB mount) sees
    it without running a bot there.

    On any failure (yt-dlp raises, postprocess raises, or the expected
    .mp3 doesn't land) any orphan thumbnail/.part/etc. files matching
    the expected base name are removed so the destination folder doesn't
    accumulate junk from failed downloads.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Track an expected base path so we can clean up on failure even if
    # yt-dlp raises before _verify_and_finalize is reached (e.g. thumbnail
    # got written, then FFmpeg postprocess failed).
    expected_base: Path | None = None
    if filename_hint:
        base = safe_filename(filename_hint)
        if playlist_index is not None:
            base = f"{playlist_index:02d} - {base}"
        outtmpl = str(dest_dir / f"{base}.%(ext)s")
        expected_base = dest_dir / base
    else:
        prefix = f"{playlist_index:02d} - " if playlist_index is not None else ""
        outtmpl = str(dest_dir / f"{prefix}%(title)s.%(ext)s")

    # Resume: if we have an obvious expected filename and the file is already
    # there with the right duration, skip and report the existing file.
    expected_duration = _probe_expected_duration(url)
    if expected_base is not None:
        hit = _existing_match(expected_base.with_suffix(".mp3"), expected_duration)
        if hit is not None:
            log.info("skip (already have %s)", hit.name)
            return DownloadResult(
                filepath=hit,
                title=hit.stem,
                uploader=None,
                source_url=url,
                duration=expected_duration,
            )

    opts = _base_opts(outtmpl)
    opts["noplaylist"] = True

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise DownloadError(f"No info returned for {url}")
            reported = ydl.prepare_filename(info)
            filepath = _verify_and_finalize(reported)
            # Only auto-rename when the caller didn't supply its own name —
            # tracklist / playlist hints already encode "Title - Artist".
            if filename_hint is None:
                filepath = _rename_to_preferred(filepath, info, playlist_index)
            else:
                # Belt-and-braces: even when a hint was supplied, if yt-dlp's
                # metadata unambiguously gives us track + artist AND the hint
                # looks like it's in reverse order ("Artist - Title") we flip
                # the file to canonical "Title - Artist". Protects against an
                # old caller (stale process, pre-pull entry.query call) still
                # feeding us hints in the deprecated order.
                filepath = _enforce_title_artist_order(filepath, info, playlist_index)

            # Verify we got the whole thing — ffmpeg sometimes writes a short
            # fragment and reports success. Only enforced when both the probe
            # and the metadata give us real numbers; otherwise trust the file.
            claimed = info.get("duration")
            actual = probe_duration(filepath) if claimed else None
            if claimed and actual and not _durations_match(actual, float(claimed)):
                raise DownloadError(
                    f"Duration mismatch: expected ~{int(claimed)}s, "
                    f"got {int(actual)}s (likely truncated download). "
                    f"File removed; try again."
                )
    except Exception:
        if expected_base is not None:
            _cleanup_partials(expected_base.with_suffix(".mp3"))
        else:
            try:
                _cleanup_partials(Path(reported).with_suffix(".mp3"))  # type: ignore[name-defined]
            except Exception:
                pass
        # If a truncated/mismatched mp3 already made it to disk, delete it so
        # the next attempt doesn't trip the "already have it" skip.
        try:
            if filepath.exists():  # type: ignore[name-defined]
                filepath.unlink()  # type: ignore[name-defined]
        except (NameError, OSError):
            pass
        raise
    else:
        _mirror_copy(filepath, _primary_root())
        try:
            from . import state as _state  # late import to avoid cycle at module load
            _state.record_download(filepath, info.get("duration"))
        except Exception:  # noqa: BLE001
            pass
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


def download_search_with_fallbacks(
    artist: str | None,
    title: str | None,
    raw_text: str,
    variant: str | None = None,
    *,
    dest_dir: Path,
    playlist_index: int | None = None,
    filename_hint: str | None = None,
) -> DownloadResult:
    """Try `download_search` with progressively looser variants of the query.

    This is what the bot calls for each tracklist entry: if the exact
    "Artist - Title (Remix) feat. X" string returns nothing, we retry with
    the feat. stripped, then the parens stripped, then a flattened query,
    then the raw tracklist line. The first success wins; if every variant
    fails we raise the LAST error so the failure report has the most-
    informative message.

    Version qualifier matching stays enforced on every attempt — we never
    silently fall back to "the original mix" when the user asked for a VIP.
    """
    from .tracklist import search_variants

    errors: list[str] = []
    variants = search_variants(artist, title, raw_text)
    for i, (a, t) in enumerate(variants):
        if a is None and t is None:
            continue
        try:
            if i > 0:
                attempt_desc = f"{a or '?'} - {t or '?'}"
                log.info("fallback search variant %d: %s", i + 1, attempt_desc)
            return download_search(
                title=t,
                artist=a,
                variant=variant,
                dest_dir=dest_dir,
                playlist_index=playlist_index,
                filename_hint=filename_hint,
            )
        except DownloadError as e:
            errors.append(f"[variant {i + 1}: {a or '-'} / {t or '-'}] {e}")
            continue
    raise DownloadError(
        "All search variants failed:\n  " + "\n  ".join(errors)
        if errors
        else "No searchable query from this entry."
    )


def extract_playlist_entries(url: str) -> tuple[str, list[dict]]:
    """Return (playlist_title, [entry_info,...]) without downloading."""
    with YoutubeDL(_probe_opts()) as ydl:
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
    """Download each entry into dest_dir, numbered by playlist order.

    We deliberately pass `filename_hint=None` so download_url's post-download
    auto-rename kicks in, producing "Title - Artist.mp3" from yt-dlp's track
    metadata. The yt-dlp playlist entry's own `title` is just the raw video
    title (often "Artist - Title" or just the song name), which would short-
    circuit the auto-rename and leave files inconsistently formatted.
    """
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
                filename_hint=None,
                playlist_index=idx,
            )
            results.append(result)
            log.info("[%d] %s -> %s", idx, title or url, result.filepath)
        except DownloadError as e:
            log.error("[%d] failed %s: %s", idx, title or url, e)
    return results


def get_video_metadata(url: str, *, with_comments: bool = False) -> dict:
    """Return raw info dict for a URL (single video, no download).

    When `with_comments=True`, also fetches top-level comments so pinned /
    author-highlighted tracklist comments become visible to
    parse_tracklist_from_info. Default off because comment extraction adds a
    second round-trip and we only want it for likely DJ sets.
    """
    opts = _probe_opts()
    opts.pop("extract_flat", None)
    opts["noplaylist"] = True
    if with_comments:
        opts["getcomments"] = True
        opts["extractor_args"] = {
            "youtube": {
                "comment_sort": ["top"],
                "max_comments": ["50", "all", "0", "0"],
            }
        }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info or {}


def is_spotify_url(url: str) -> bool:
    lower = url.lower()
    return any(host in lower for host in SPOTIFY_HOSTS)
