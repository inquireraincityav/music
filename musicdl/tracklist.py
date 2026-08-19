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

_ARTIST_TITLE_SEPARATORS = (" - ", " – ", " — ")


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
    """Best-effort split on ' - ' (or unicode dash) into (artist, title)."""
    cleaned = _NUMBER_PREFIX.sub("", text).strip()
    for sep in _ARTIST_TITLE_SEPARATORS:
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            left, right = left.strip(), right.strip()
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


def parse_tracklist_from_info(info: dict) -> list[TracklistEntry]:
    """Try description first, then comments (if fetched)."""
    description = info.get("description") or ""
    entries = parse_tracklist(description)
    if entries:
        return entries
    for c in info.get("comments") or []:
        body = c.get("text") or ""
        entries = parse_tracklist(body)
        if entries:
            return entries
    return []
