"""Parse tracklists from YouTube DJ-set descriptions or comments.

Three layered formats are supported. `parse_tracklist` tries them in order
and returns the first that yields entries:

  1. Timestamped: "01:23 Artist - Title" (or timestamp trailing).
  2. Numbered:    "1. Artist - Title", "01) Artist - Title",
                  "Track 1 - Artist - Title".
  3. Plain-section: bare "Artist - Title" lines under a header like
                    "Tracklist:", "Setlist:", "Track IDs:".
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ---- Timestamped-line patterns (original behavior) ----

_TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
_TIMESTAMP_LINE_PATTERNS = [
    re.compile(rf"^\s*[\[\(]?\s*(?P<ts>{_TS})\s*[\]\)]?\s*[-–—.:]?\s*(?P<txt>.+?)\s*$"),
    re.compile(rf"^\s*(?P<txt>.+?)\s+[\[\(]?\s*(?P<ts>{_TS})\s*[\]\)]?\s*$"),
]

# ---- Numbered-list pattern ----
#
# Matches things like:
#   "1. Artist - Title"
#   "01) Artist - Title"
#   "01 - Artist - Title"
#   "01: Artist - Title"
#   "Track 1 - Artist - Title"
#   "[01] Artist - Title"

_NUMBERED_LINE = re.compile(
    r"^\s*(?:track\s+)?[\[\(]?\s*(?P<num>\d{1,3})\s*[\]\)\.\:\-]\s+(?P<txt>.+?)\s*$",
    re.IGNORECASE,
)

# ---- Tracklist section headers ----
#
# Anywhere in a line, matches "tracklist", "setlist", "track list", "track ids"
# etc. (word boundary so we don't false-match inside longer words).

_TRACKLIST_HEADER = re.compile(
    r"\b(?:tracklist|track\s*list|setlist|set\s*list|track\s*ids?|songs?)\b",
    re.IGNORECASE,
)

# ---- Number prefixes stripped when parsing artist/title ----

_NUMBER_PREFIX = re.compile(r"^\s*\d{1,3}\s*[\.\)\-:]\s+")

# ---- Noise lines skipped in the timestamped parser (header lines) ----

_NOISE = re.compile(
    r"^(tracklist|track\s*list|setlist|set\s*list|chapters?)\b",
    re.IGNORECASE,
)

# ---- Separators we consider between artist and title ----
#
# Order matters — most-specific first. A unicode em-dash should win over a
# hyphen that also appears inside the title ("Fisher - Losing It - VIP Edit").

_ARTIST_TITLE_SEPARATORS = (
    " - ", " – ", " — ", " ~ ", " | ",
    # ascii "artist | title" and "artist :: title" variants seen in the wild
    " :: ",
)


@dataclass
class TracklistEntry:
    index: int
    timestamp: str
    seconds: int
    text: str
    artist: str | None
    title: str | None

    @property
    def query(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.text

    @property
    def filename(self) -> str:
        """Preferred on-disk stem: 'Title - Artist' when both are known.

        Logs every call so misbehaving installs (old venv, stale process, two
        musicdl packages on sys.path) show up in the bot log immediately.
        """
        if self.artist and self.title:
            out = f"{self.title} - {self.artist}"
            import logging
            logging.getLogger("musicdl.tracklist").info(
                "FILENAME-ORDER=Title-Artist stem=%r", out
            )
            return out
        return self.text


def _ts_to_seconds(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    return 0


def _split_artist_title(text: str) -> tuple[str | None, str | None]:
    """Best-effort split on ' - ' (or unicode dash) into (artist, title).

    Splits on the EARLIEST separator by position, not by separator-list order.
    This matters for lines like "Drake – Best I Ever Had - EYJEY Flip" where
    both ' – ' (en-dash, position 5) and ' - ' (ascii, position 23) are
    present: the en-dash wins because it's the artist/title boundary; the
    ascii hyphen is a loose version separator inside the title.
    """
    cleaned = _NUMBER_PREFIX.sub("", text).strip()
    best_idx = -1
    best_sep = ""
    for sep in _ARTIST_TITLE_SEPARATORS:
        idx = cleaned.find(sep)
        if idx != -1 and (best_idx == -1 or idx < best_idx):
            best_idx = idx
            best_sep = sep
    if best_idx != -1:
        left = cleaned[:best_idx].strip()
        right = cleaned[best_idx + len(best_sep):].strip()
        if left and right:
            return left, right
    return None, cleaned or None


def _has_artist_title_sep(text: str) -> bool:
    return any(sep in text for sep in _ARTIST_TITLE_SEPARATORS)


def _clean_txt(text: str) -> str:
    return text.strip().strip('"').strip("'").strip()


# ---- Parsers ----


def _parse_timestamped(text: str) -> list[TracklistEntry]:
    entries: list[TracklistEntry] = []
    seen_ts: set[int] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _NOISE.match(line):
            continue
        matched: re.Match | None = None
        for pat in _TIMESTAMP_LINE_PATTERNS:
            m = pat.match(line)
            if m:
                matched = m
                break
        if not matched:
            continue
        ts = matched.group("ts")
        txt = _clean_txt(matched.group("txt"))
        if not txt:
            continue
        secs = _ts_to_seconds(ts)
        if secs in seen_ts:
            continue
        seen_ts.add(secs)
        artist, title = _split_artist_title(txt)
        entries.append(
            TracklistEntry(
                index=0,
                timestamp=ts,
                seconds=secs,
                text=txt,
                artist=artist,
                title=title,
            )
        )
    entries.sort(key=lambda e: e.seconds)
    for i, e in enumerate(entries, 1):
        e.index = i
    return entries


def _parse_numbered(text: str) -> list[TracklistEntry]:
    entries: list[TracklistEntry] = []
    seen_nums: set[int] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _NUMBERED_LINE.match(line)
        if not m:
            continue
        num = int(m.group("num"))
        txt = _clean_txt(m.group("txt"))
        if not txt or not _has_artist_title_sep(txt):
            continue
        if num in seen_nums:
            continue
        seen_nums.add(num)
        artist, title = _split_artist_title(txt)
        entries.append(
            TracklistEntry(
                index=len(entries) + 1,
                timestamp="00:00",
                seconds=0,
                text=txt,
                artist=artist,
                title=title,
            )
        )
    if len(entries) < 2:
        # A single numbered line is more likely noise than a tracklist.
        return []
    return entries


def _parse_plain_section(text: str) -> list[TracklistEntry]:
    entries: list[TracklistEntry] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not in_section:
            if line and _TRACKLIST_HEADER.search(line):
                in_section = True
            continue
        # We're inside a tracklist section.
        if not line:
            # Blank lines are OK inside the section (some descriptions
            # separate blocks). Keep collecting.
            continue
        stripped = _NUMBER_PREFIX.sub("", line).strip()
        if not _has_artist_title_sep(stripped):
            # Non-track line inside the section — assume the section ended.
            if entries:
                break
            continue
        artist, title = _split_artist_title(stripped)
        entries.append(
            TracklistEntry(
                index=len(entries) + 1,
                timestamp="00:00",
                seconds=0,
                text=stripped,
                artist=artist,
                title=title,
            )
        )
    if len(entries) < 2:
        return []
    return entries


# ---- Public API ----


def parse_tracklist(text: str) -> list[TracklistEntry]:
    """Try each parser in order until one yields entries."""
    if not text:
        return []
    for parser in (_parse_timestamped, _parse_numbered, _parse_plain_section):
        entries = parser(text)
        if entries:
            return entries
    return []


def _entries_from_chapters(info: dict) -> list[TracklistEntry]:
    """YouTube chapter list → tracklist entries.

    yt-dlp exposes chapters as [{"title": "Artist - Song", "start_time": 83.0}, ...].
    These are the most reliable source when they're present — the uploader
    structured them deliberately.
    """
    chapters = info.get("chapters") or []
    out: list[TracklistEntry] = []
    for i, c in enumerate(chapters, 1):
        title = (c.get("title") or "").strip()
        if not title:
            continue
        start = int(c.get("start_time") or 0)
        ts = f"{start // 60:02d}:{start % 60:02d}" if start < 3600 else (
            f"{start // 3600:d}:{(start % 3600) // 60:02d}:{start % 60:02d}"
        )
        artist, song = _split_artist_title(title)
        out.append(
            TracklistEntry(
                index=i,
                timestamp=ts,
                seconds=start,
                text=title,
                artist=artist,
                title=song,
            )
        )
    # A single chapter is almost always "Intro" and not a tracklist.
    return out if len(out) >= 2 else []


def _pinned_first(comments: list[dict]) -> list[dict]:
    """Return comments with pinned/highlighted ones first — DJ sets' tracklists
    are usually pinned by the uploader when they're in a comment."""
    pinned, rest = [], []
    for c in comments:
        if c.get("is_pinned") or c.get("is_favorited") or c.get("author_is_uploader"):
            pinned.append(c)
        else:
            rest.append(c)
    return pinned + rest


def parse_tracklist_from_info(info: dict) -> list[TracklistEntry]:
    """Try chapters first (highest signal), then description, then comments."""
    chapter_entries = _entries_from_chapters(info)
    if chapter_entries:
        return chapter_entries

    description = info.get("description") or ""
    entries = parse_tracklist(description)
    if entries:
        return entries

    for c in _pinned_first(info.get("comments") or []):
        body = c.get("text") or ""
        entries = parse_tracklist(body)
        if entries:
            return entries
    return []


# ---- Search-variant fallbacks for stubborn tracks ----
#
# When a tracklist entry's primary query fails to find a download, we retry
# with increasingly loose versions of the same query before giving up. Ordered
# from "most faithful" to "last resort" — the caller stops as soon as one
# succeeds, so the retained variant stays as close as possible to what the
# user asked for.

_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")
# `feat. X`, `ft. X`, `ft X` (no period), `featuring X`.
_FEAT = re.compile(
    r"\s*(?:feat\.?|ft\.?|featuring)\s+[^-–—()\[\]]+",
    re.IGNORECASE,
)
# Timestamp prefix like `12:34 ` or `12:34. ` at the start of a line.
_LEADING_TS = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s*[\.\-:]?\s+")


def _strip_leading_timestamp(text: str) -> str:
    return _LEADING_TS.sub("", text).strip()


def search_variants(
    artist: str | None,
    title: str | None,
    text: str,
) -> list[tuple[str | None, str | None]]:
    """Progressive fallbacks for a single track. Each item is (artist, title)
    passed to download_search; the first success wins.

    Order is tuned around a user observation: on messy tracklist lines the
    *raw whole line* often matches YouTube/SoundCloud uploads better than
    anything our structured splitter produces (uploaders name their files
    exactly like the DJ's tracklist entry). So the whole-line attempt sits
    second — right after the structured query, before we start stripping
    bits off.

    Example on "T-Pain – Buy You A Drank (Dan Bravo Remix)":
      1. ("T-Pain",  "Buy You A Drank (Dan Bravo Remix)")   # structured
      2. (None,      "T-Pain Buy You A Drank (Dan Bravo Remix)")  # raw line
      3. ("T-Pain",  "Buy You A Drank Dan Bravo Remix")     # parens unwrapped
      4. ("T-Pain",  "Buy You A Drank")                     # parens dropped
      5. (None,      "T-Pain Buy You A Drank")              # flattened
    """
    seen: set[tuple[str | None, str | None]] = set()
    out: list[tuple[str | None, str | None]] = []

    def add(a: str | None, t: str | None) -> None:
        a_s = (a or "").strip() or None
        t_s = (t or "").strip() or None
        if not t_s and not a_s:
            return
        key = (a_s, t_s)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    # 1. The structured query (what the splitter gave us).
    add(artist, title)

    # 2. The RAW whole tracklist line — timestamps stripped if present.
    #    Users report this often finds uploads named exactly like the line.
    if text:
        add(None, _strip_leading_timestamp(text))

    if title:
        # 3. Keep version info but unwrap parens — some uploads list the mix
        #    inline without brackets, e.g. "Dan Bravo Remix" not "(Dan Bravo Remix)".
        unwrapped = re.sub(r"[\(\[\)\]]", "", title)
        unwrapped = re.sub(r"\s+", " ", unwrapped).strip()
        add(artist, unwrapped)
        # 4. Strip "feat. X" / "ft X" phrasing from the title.
        no_feat = _FEAT.sub("", title).strip()
        if no_feat != title:
            add(artist, no_feat)
        # 5. Strip "(…)" blocks entirely — most permissive, most likely to
        #    land on the wrong version (that's what version-hint matching is
        #    for in the downloader).
        no_paren = _PAREN.sub(" ", no_feat).strip()
        add(artist, no_paren)
        # 6. Flattened artist + title (handles collaborations, "x", "&").
        if artist:
            flat = f"{artist} {no_paren}".strip()
            add(None, flat)

    return out
