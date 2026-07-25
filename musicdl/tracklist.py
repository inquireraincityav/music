"""Parse timestamped tracklists from YouTube DJ-set descriptions or comments."""
from __future__ import annotations

import re
from dataclasses import dataclass


# Matches timestamps like "1:23", "01:23", "1:23:45" at the start of an item.
_TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"

# A line-level regex. Timestamp may lead, trail, or be in brackets.
_LINE_PATTERNS = [
    re.compile(rf"^\s*[\[\(]?\s*(?P<ts>{_TS})\s*[\]\)]?\s*[-–—.:]?\s*(?P<txt>.+?)\s*$"),
    re.compile(rf"^\s*(?P<txt>.+?)\s+[\[\(]?\s*(?P<ts>{_TS})\s*[\]\)]?\s*$"),
]

# Track number prefixes ("01. ", "1) ", "01 - ") we strip from the text side.
_NUMBER_PREFIX = re.compile(r"^\s*\d{1,3}\s*[\.\)\-:]\s+")

# Noise lines we skip even if they parse (chapter headers etc.).
_NOISE = re.compile(
    r"^(tracklist|track\s*list|setlist|set\s*list|chapters?)\b",
    re.IGNORECASE,
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
    for sep in (" - ", " – ", " — "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return None, cleaned or None


def parse_tracklist(text: str) -> list[TracklistEntry]:
    """Extract a list of tracks with timestamps from a description/comment blob."""
    entries: list[TracklistEntry] = []
    seen_ts: set[int] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _NOISE.match(line):
            continue
        matched: re.Match | None = None
        for pat in _LINE_PATTERNS:
            m = pat.match(line)
            if m:
                matched = m
                break
        if not matched:
            continue
        ts = matched.group("ts")
        txt = matched.group("txt").strip()
        # Drop enclosing quotes.
        txt = txt.strip('"').strip("'").strip()
        if not txt:
            continue
        secs = _ts_to_seconds(ts)
        if secs in seen_ts:
            continue
        seen_ts.add(secs)
        artist, title = _split_artist_title(txt)
        entries.append(
            TracklistEntry(
                index=0,  # filled after sort
                timestamp=ts,
                seconds=secs,
                text=txt,
                artist=artist,
                title=title,
            )
        )

    # Sort by timestamp and number them 1..N.
    entries.sort(key=lambda e: e.seconds)
    for i, e in enumerate(entries, 1):
        e.index = i
    return entries


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
