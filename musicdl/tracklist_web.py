"""Web fallback for discovering DJ-set tracklists.

Layered strategy used by !set when the video description/comments don't yield
a parseable tracklist:

  1. Look for a 1001tracklists.com URL in the video description.
  2. If none found, run a DuckDuckGo HTML search for the video title.
  3. Try to fetch the discovered URL with plain requests + browser headers.
  4. If Cloudflare's bot-check page comes back instead, retry via Playwright
     (headless Chromium) — only if playwright is installed.
  5. Parse the HTML for tracks (schema.org JSON-LD if present, else known
     1001tl CSS classes, else a broad heuristic).

Everything degrades gracefully: any failure returns an empty tracklist and the
caller falls back to asking the user to paste one manually.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from .tracklist import TracklistEntry, _split_artist_title

log = logging.getLogger("musicdl.tracklist_web")


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
}

_1001_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?1001tracklists\.com/tracklist/[^\s\"'<>]+",
    re.IGNORECASE,
)

# Any of these markers in a response body means Cloudflare blocked us.
_CLOUDFLARE_MARKERS = (
    "Just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "Attention Required! | Cloudflare",
    "Please enable JS and disable any ad blocker",
)


@dataclass
class DiscoveryResult:
    tracks: list[TracklistEntry]
    source_url: Optional[str]
    notes: list[str]  # human-readable trail of what was tried


# ---------- URL/text scanning ----------


def find_1001tracklists_url_in_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = _1001_URL_RE.search(text)
    return m.group(0) if m else None


# ---------- Cloudflare detection ----------


def _looks_like_cloudflare(html: str) -> bool:
    if not html:
        return True
    lowered = html.lower()
    return any(marker.lower() in lowered for marker in _CLOUDFLARE_MARKERS)


# ---------- Simple HTTP fetch ----------


def _requests_get(url: str, timeout: int = 12) -> Optional[str]:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    except requests.RequestException as e:
        log.warning("requests.get failed for %s: %s", url, e)
        return None
    if resp.status_code >= 400:
        log.warning("requests.get %d for %s", resp.status_code, url)
        return None
    return resp.text


# ---------- Optional Playwright fetch ----------


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def _playwright_get(url: str, timeout: int = 30) -> Optional[str]:
    """Fetch url with a headless browser. Returns None on any failure."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        log.debug("playwright not installed: %s", e)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=_USER_AGENT)
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # Give Cloudflare a moment to auto-resolve if it appears.
                try:
                    page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                except Exception:
                    pass
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        log.warning("playwright fetch failed for %s: %s", url, e)
        return None


def _fetch_html(url: str) -> tuple[Optional[str], list[str]]:
    """Return (html, notes). Tries requests then optionally playwright."""
    notes: list[str] = []
    html = _requests_get(url)
    if html and not _looks_like_cloudflare(html):
        notes.append(f"fetched {url} via requests")
        return html, notes
    if html and _looks_like_cloudflare(html):
        notes.append("Cloudflare challenge on requests fetch")
    if not html:
        notes.append("requests fetch returned nothing")

    if _playwright_available():
        notes.append("retrying via Playwright (headless Chromium)")
        html = _playwright_get(url)
        if html and not _looks_like_cloudflare(html):
            notes.append("fetched via Playwright")
            return html, notes
        notes.append("Playwright also failed or hit Cloudflare")
    else:
        notes.append(
            "Playwright not installed; skipping browser fallback "
            "(install with: pip install playwright && playwright install chromium)"
        )
    return None, notes


# ---------- Web search (DuckDuckGo HTML) ----------


def _duckduckgo_first_1001tl(query: str) -> Optional[str]:
    """Search DuckDuckGo HTML endpoint and return the first 1001tracklists URL."""
    q = quote_plus(f"{query} 1001tracklists")
    html = _requests_get(
        f"https://duckduckgo.com/html/?q={q}", timeout=12
    )
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    # DDG's HTML results use <a class="result__a" href="...">
    for a in soup.select("a.result__a, a"):
        href = a.get("href") or ""
        # DDG wraps links in a redirector; look for 1001tl anywhere in href.
        m = _1001_URL_RE.search(href)
        if m:
            return m.group(0)
    return None


# ---------- HTML parsing ----------


def _parse_json_ld(soup: BeautifulSoup) -> list[TracklistEntry]:
    """Look for JSON-LD MusicPlaylist / ItemList entries."""
    entries: list[TracklistEntry] = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = _extract_items_from_jsonld(data)
        for i, txt in enumerate(items, len(entries) + 1):
            artist, title = _split_artist_title(txt)
            entries.append(
                TracklistEntry(
                    index=i,
                    timestamp="00:00",
                    seconds=0,
                    text=txt,
                    artist=artist,
                    title=title,
                )
            )
        if entries:
            break
    return entries


def _extract_items_from_jsonld(data) -> list[str]:
    """Walk a JSON-LD blob for anything that looks like track strings."""
    results: list[str] = []
    if isinstance(data, list):
        for d in data:
            results.extend(_extract_items_from_jsonld(d))
        return results
    if not isinstance(data, dict):
        return results
    t = data.get("@type") or ""
    types = t if isinstance(t, list) else [t]
    if "MusicPlaylist" in types or "ItemList" in types:
        for it in data.get("track") or data.get("itemListElement") or []:
            if isinstance(it, dict):
                name = it.get("name") or (
                    it.get("item") or {}
                ).get("name") if isinstance(it.get("item"), dict) else None
                by = it.get("byArtist") or (
                    it.get("item") or {}
                ).get("byArtist") if isinstance(it.get("item"), dict) else None
                if isinstance(by, dict):
                    by = by.get("name")
                if isinstance(by, list):
                    by = ", ".join(x.get("name", "") for x in by if isinstance(x, dict))
                if name and by:
                    results.append(f"{by} - {name}")
                elif name:
                    results.append(name)
    return results


def _parse_1001tl_css(soup: BeautifulSoup) -> list[TracklistEntry]:
    """Parse 1001tracklists' HTML via known CSS class patterns."""
    entries: list[TracklistEntry] = []
    # Track rows on 1001tl are typically <div class="tlpItem"> containing
    # anchors for artist and title. Fall back to any [data-trno].
    rows = soup.select("div.tlpItem") or soup.select("[data-trno]")
    for i, row in enumerate(rows, 1):
        text = row.get_text(" ", strip=True)
        if not text:
            continue
        artist, title = _split_artist_title(text)
        entries.append(
            TracklistEntry(
                index=i,
                timestamp="00:00",
                seconds=0,
                text=text,
                artist=artist,
                title=title,
            )
        )
    return entries


def _parse_heuristic(soup: BeautifulSoup) -> list[TracklistEntry]:
    """Last-ditch parse: pull text from any element whose class hints 'track'."""
    entries: list[TracklistEntry] = []
    seen: set[str] = set()
    for el in soup.select("[class*='track'], [class*='tlp']"):
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 300 or " - " not in text:
            continue
        if text in seen:
            continue
        seen.add(text)
        artist, title = _split_artist_title(text)
        entries.append(
            TracklistEntry(
                index=len(entries) + 1,
                timestamp="00:00",
                seconds=0,
                text=text,
                artist=artist,
                title=title,
            )
        )
    return entries


def parse_1001tracklists_html(html: str) -> list[TracklistEntry]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    for parser in (_parse_json_ld, _parse_1001tl_css, _parse_heuristic):
        entries = parser(soup)
        if entries:
            return entries
    return []


# ---------- Orchestration ----------


def discover_tracklist_from_web(info: dict) -> DiscoveryResult:
    """Try to find a tracklist for the video represented by yt-dlp `info`."""
    notes: list[str] = []
    description = info.get("description") or ""
    title = info.get("title") or ""

    tl_url = find_1001tracklists_url_in_text(description)
    if tl_url:
        notes.append(f"1001tracklists URL found in description: {tl_url}")
    else:
        if title:
            notes.append(f"searching DuckDuckGo for '{title}' 1001tracklists")
            tl_url = _duckduckgo_first_1001tl(title)
            if tl_url:
                notes.append(f"web search found: {tl_url}")
            else:
                notes.append("web search returned no 1001tracklists hits")

    if not tl_url:
        return DiscoveryResult(tracks=[], source_url=None, notes=notes)

    html, fetch_notes = _fetch_html(tl_url)
    notes.extend(fetch_notes)
    if not html:
        return DiscoveryResult(tracks=[], source_url=tl_url, notes=notes)

    tracks = parse_1001tracklists_html(html)
    if tracks:
        notes.append(f"parsed {len(tracks)} tracks from page")
    else:
        notes.append("page fetched but no tracks parsed (site layout may have changed)")
    return DiscoveryResult(tracks=tracks, source_url=tl_url, notes=notes)
