"""Re-tag existing MP3s in place with the Serato-friendly header set.

Stream-copies the audio (no re-encode, no quality loss) and rewrites just
the container headers:

  - ID3v2.3 tags (not ffmpeg's default v2.4; Serato reads v2.3 reliably).
  - Xing/Info header forced on (accurate frame count up-front).

Does NOT change the sample rate — that would require a full re-encode
(small lossy pass). If you want 44.1 kHz on older files too, re-download
them; the new encoder config already resamples at download time.

Usage:
    musicdl-retag                     # retag everything under the default output dir
    musicdl-retag --dir /path         # retag a different folder
    musicdl-retag --delete-serato     # also delete .serato sidecar folders so Serato re-analyzes
    musicdl-retag --dry-run           # print what would change, don't touch anything
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("musicdl.retag")


def _ffmpeg_binary() -> str:
    """Resolve ffmpeg: bundled copy first, PATH second."""
    try:
        from .downloader import _bundled_ffmpeg_dir
        bundled = _bundled_ffmpeg_dir()
        if bundled:
            exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
            return str(Path(bundled) / exe)
    except Exception:
        pass
    return shutil.which("ffmpeg") or "ffmpeg"


def retag_file(mp3: Path, dry_run: bool = False) -> tuple[bool, str]:
    """Rewrite one MP3's headers. Returns (success, message)."""
    if dry_run:
        return True, "would retag"

    tmp = mp3.with_suffix(mp3.suffix + ".retag.tmp")
    cmd = [
        _ffmpeg_binary(),
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(mp3),
        "-map", "0",            # keep every stream (audio + embedded art)
        "-c:a", "copy",         # stream-copy audio: no re-encode, no quality loss
        "-c:v", "copy",         # keep embedded cover art
        "-id3v2_version", "3",
        "-write_xing", "1",
        "-f", "mp3",
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return False, "ffmpeg timeout (file skipped)"
    except FileNotFoundError:
        return False, "ffmpeg not found"
    if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        err = (result.stderr or result.stdout or "ffmpeg exit nonzero").strip()
        return False, err.splitlines()[-1][:200]

    # Preserve mtime so Finder sort / Serato last-modified logic doesn't
    # treat the file as freshly added.
    try:
        st = mp3.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    try:
        tmp.replace(mp3)  # atomic swap
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return False, f"rename failed: {e}"
    return True, "ok"


def retag_tree(
    root: Path,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    """Retag every .mp3 under root (recursive). Returns stats dict."""
    mp3s = sorted(root.rglob("*.mp3"))
    stats: dict = {
        "total": len(mp3s),
        "ok": 0,
        "failed": 0,
        "errors": [],  # list[tuple[str, str]]
    }
    for i, mp3 in enumerate(mp3s, 1):
        if progress_cb:
            progress_cb(i, len(mp3s), mp3)
        ok, msg = retag_file(mp3, dry_run=dry_run)
        if ok:
            stats["ok"] += 1
            log.debug("retagged %s", mp3)
        else:
            stats["failed"] += 1
            stats["errors"].append((str(mp3), msg))
            log.warning("failed %s: %s", mp3, msg)
    return stats


def delete_serato_sidecars(root: Path, dry_run: bool = False) -> int:
    """Delete every .serato analysis folder under root so Serato re-analyzes
    with the new headers on next add. Returns count removed (or would-remove)."""
    count = 0
    for p in root.rglob(".serato"):
        if not p.is_dir():
            continue
        count += 1
        if not dry_run:
            shutil.rmtree(p, ignore_errors=True)
    return count


def _default_root() -> Path:
    return Path(
        os.environ.get("MUSICDL_OUTPUT_DIR")
        or (Path.home() / "Desktop" / "MusicDownloads")
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite existing MP3s' headers for Serato (no re-encode).",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Root folder to walk. Default: $MUSICDL_OUTPUT_DIR or ~/Desktop/MusicDownloads",
    )
    parser.add_argument(
        "--delete-serato",
        action="store_true",
        help="Also delete .serato analysis sidecar folders so Serato re-analyzes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would change; don't touch any files.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    root = Path(args.dir) if args.dir else _default_root()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    mp3s = list(root.rglob("*.mp3"))
    if not mp3s:
        print(f"No .mp3 files under {root}")
        return 0

    print(f"Found {len(mp3s)} .mp3 files under {root}")
    if args.dry_run:
        print("DRY RUN — no files will change")
    print("Re-tagging with ID3v2.3 + Xing header (stream copy, no quality loss)...")

    def progress(i: int, n: int, p: Path) -> None:
        # Only redraw once a second to avoid spamming terminals.
        if i == n or i % max(1, n // 100) == 0:
            print(f"\r  [{i}/{n}] {p.name[:60]:<60}", end="", flush=True)

    stats = retag_tree(root, dry_run=args.dry_run, progress_cb=progress)
    print()  # newline after the progress line

    print(f"✓ Re-tagged: {stats['ok']} / {stats['total']}")
    if stats["failed"]:
        print(f"✗ Failed:    {stats['failed']}")
        for path, err in stats["errors"][:10]:
            print(f"    {Path(path).name}: {err}")
        if len(stats["errors"]) > 10:
            print(f"    …and {len(stats['errors']) - 10} more (see log)")

    if args.delete_serato:
        n = delete_serato_sidecars(root, dry_run=args.dry_run)
        verb = "would delete" if args.dry_run else "deleted"
        print(f"{verb} {n} .serato sidecar folder(s) — Serato will re-analyze on next add")
    else:
        print(
            "\nNote: Serato caches analysis per file in a .serato folder next to it. "
            "To make Serato re-render waveforms with the new headers, either re-run "
            "with --delete-serato, or in Serato right-click the files and 'Rescan ID3 "
            "tags' then re-add the folder."
        )

    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
