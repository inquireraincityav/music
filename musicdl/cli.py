from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .downloader import (
    download_playlist_entries,
    download_search,
    download_url,
    extract_playlist_entries,
    get_video_metadata,
    is_spotify_url,
)
from .organizer import playlist_dir, set_dir, single_dir
from .spotify import download_spotify
from .tracklist import parse_tracklist_from_info

log = logging.getLogger("musicdl")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _is_playlist_url(info: dict) -> bool:
    if info.get("_type") == "playlist":
        return True
    entries = info.get("entries")
    return bool(entries) and len(list(entries)) > 1


def _run_set(url: str, root: Path | None, name_override: str | None) -> int:
    """Fetch a video's info, parse tracklist, download each track by search."""
    log.info("Fetching set metadata: %s", url)
    info = get_video_metadata(url)
    if not info:
        log.error("Could not fetch metadata for %s", url)
        return 2
    tracks = parse_tracklist_from_info(info)
    if not tracks:
        log.error(
            "No tracklist found in description/comments for %s. "
            "Try passing --tracklist-file with a manual list.",
            url,
        )
        return 3
    name = name_override or info.get("title") or "set"
    out = set_dir(name, root)
    log.info("Parsed %d tracks. Downloading into %s", len(tracks), out)
    failed: list[str] = []
    for entry in tracks:
        try:
            download_search(
                title=entry.title,
                artist=entry.artist,
                variant=None,
                dest_dir=out,
                playlist_index=entry.index,
                filename_hint=entry.query,
            )
        except Exception as e:
            log.error("[%d] failed %s: %s", entry.index, entry.query, e)
            failed.append(entry.query)
    if failed:
        log.warning("Failed tracks (%d): %s", len(failed), failed)
    return 0 if not failed else 4


def _run_tracklist_file(path: Path, root: Path, name: str) -> int:
    from .tracklist import parse_tracklist

    text = path.read_text(encoding="utf-8")
    tracks = parse_tracklist(text)
    if not tracks:
        # Fall back: treat each non-empty line as "Artist - Title".
        from .tracklist import TracklistEntry, _split_artist_title

        tracks = []
        for i, line in enumerate(
            (ln.strip() for ln in text.splitlines() if ln.strip()), 1
        ):
            artist, title = _split_artist_title(line)
            tracks.append(
                TracklistEntry(
                    index=i,
                    timestamp="00:00",
                    seconds=0,
                    text=line,
                    artist=artist,
                    title=title,
                )
            )
    if not tracks:
        log.error("Tracklist file is empty.")
        return 3
    out = set_dir(name, root)
    failed: list[str] = []
    for entry in tracks:
        try:
            download_search(
                title=entry.title,
                artist=entry.artist,
                variant=None,
                dest_dir=out,
                playlist_index=entry.index,
                filename_hint=entry.query,
            )
        except Exception as e:
            log.error("[%d] failed %s: %s", entry.index, entry.query, e)
            failed.append(entry.query)
    return 0 if not failed else 4


def _run_url(url: str, root: Path | None, force_playlist: bool) -> int:
    if is_spotify_url(url):
        # spotdl decides between single/playlist itself; drop into a folder
        # named after Spotify's response (via {list-name} isn't guaranteed,
        # so just group under a stable directory).
        out = single_dir(root) if "/track/" in url else playlist_dir("Spotify", root)
        return download_spotify(url, out)

    log.info("Inspecting %s", url)
    info = get_video_metadata(url)
    if force_playlist or _is_playlist_url(info):
        name, entries = extract_playlist_entries(url)
        if not entries:
            log.error("No entries found in playlist %s", url)
            return 2
        out = playlist_dir(name, root)
        log.info("Playlist %r: %d entries -> %s", name, len(entries), out)
        results = download_playlist_entries(entries, out)
        return 0 if results else 4

    out = single_dir(root)
    result = download_url(url, dest_dir=out)
    log.info("Saved %s", result.filepath)
    return 0


def _run_search(args: argparse.Namespace, root: Path | None) -> int:
    out = single_dir(root)
    result = download_search(
        title=args.title or args.query,
        artist=args.artist,
        variant=args.variant,
        dest_dir=out,
        filename_hint=None,
    )
    log.info("Saved %s", result.filepath)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="musicdl",
        description=(
            "Fetch songs, playlists, or DJ-set tracklists as 320 kbps MP3. "
            "Pass a URL (YouTube / SoundCloud / Spotify / etc.) OR a search query."
        ),
    )
    p.add_argument(
        "query",
        nargs="?",
        help="URL or free-text search query (e.g. \"Artist - Title (Extended Mix)\").",
    )
    p.add_argument("--title", help="Track title (used with search / to disambiguate).")
    p.add_argument("--artist", help="Track artist.")
    p.add_argument(
        "--variant",
        help='Version qualifier ("Extended Mix", "VIP Edit", remixer name, etc.).',
    )
    p.add_argument(
        "--set",
        dest="is_set",
        action="store_true",
        help="Treat the URL as a DJ set and parse its tracklist for per-track downloads.",
    )
    p.add_argument(
        "--playlist",
        dest="force_playlist",
        action="store_true",
        help="Force playlist mode even if the URL looks like a single video.",
    )
    p.add_argument(
        "--tracklist-file",
        type=Path,
        help="Path to a text file with one 'Artist - Title' per line (or timestamped).",
    )
    p.add_argument(
        "--name",
        help="Folder name to use for --set or --tracklist-file output.",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Override output root (default: ~/Desktop/MusicDownloads).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    root = args.output

    if args.tracklist_file:
        name = args.name or args.tracklist_file.stem
        return _run_tracklist_file(args.tracklist_file, root, name)

    if not args.query and not (args.title or args.artist):
        parser.error("Provide a URL, a search query, or --title/--artist.")

    if args.query and _looks_like_url(args.query):
        if args.is_set:
            return _run_set(args.query, root, args.name)
        return _run_url(args.query, root, args.force_playlist)

    return _run_search(args, root)


if __name__ == "__main__":
    sys.exit(main())
