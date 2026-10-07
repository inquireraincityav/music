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
    musicdl-retag --serato-crates     # retag only files referenced by your Serato crates
    musicdl-retag --serato-crates "Hip Hop,Bangers"
                                      # limit to specific crate names (comma-separated)
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


# ---------- Serato crate parsing ----------
#
# Serato stores subcrates at ~/Music/_Serato_/Subcrates/<name>.crate (and
# per-drive at <volume>/_Serato_/Subcrates/*.crate for external volumes).
# The file is a proprietary binary stream of 4-byte tags + 4-byte big-endian
# lengths + payloads.
#
# We don't need a full parser — just the track paths. Each track entry
# contains a 'ptrk' tag whose payload is a UTF-16 BE path RELATIVE to the
# volume the crate lives on (no leading slash). We scan for every 'ptrk',
# decode its payload, and resolve it against the crate's own volume.


_SERATO_SUBCRATES_PATHS = [
    Path.home() / "Music" / "_Serato_" / "Subcrates",
]


def _volume_root_for(crate_path: Path) -> Path:
    """Serato stores track paths relative to the volume the crate is on.
    On macOS the system drive is '/' and external drives are '/Volumes/<name>'.
    We walk up the crate's parent chain until we find '_Serato_' and take
    everything above it as the volume root."""
    for parent in crate_path.parents:
        if parent.name == "_Serato_":
            return parent.parent  # the volume root
    return Path("/")


def _parse_crate_paths(crate_path: Path) -> list[Path]:
    """Extract absolute file paths from one Serato .crate file."""
    try:
        data = crate_path.read_bytes()
    except OSError:
        return []
    volume = _volume_root_for(crate_path)
    out: list[Path] = []
    seen: set[str] = set()
    i = 0
    while True:
        idx = data.find(b"ptrk", i)
        if idx == -1 or idx + 8 > len(data):
            break
        length = int.from_bytes(data[idx + 4:idx + 8], "big")
        start = idx + 8
        end = start + length
        if length <= 0 or end > len(data):
            i = idx + 4
            continue
        try:
            rel = data[start:end].decode("utf-16-be", errors="ignore").strip("\x00")
        except Exception:
            i = end
            continue
        # Serato paths are relative-to-volume, no leading slash.
        if rel and rel not in seen:
            seen.add(rel)
            out.append(volume / rel)
        i = end
    return out


def _find_crate_files(selected_names: Optional[list[str]] = None) -> list[Path]:
    """Return every .crate file Serato knows about on this machine, optionally
    narrowed to a set of names (without the .crate suffix, case-insensitive)."""
    name_filter: Optional[set[str]] = None
    if selected_names:
        name_filter = {n.strip().lower() for n in selected_names if n.strip()}

    crates: list[Path] = []
    search_dirs = list(_SERATO_SUBCRATES_PATHS)
    volumes_root = Path("/Volumes")
    if volumes_root.is_dir():
        for vol in volumes_root.iterdir():
            candidate = vol / "_Serato_" / "Subcrates"
            if candidate.is_dir():
                search_dirs.append(candidate)

    for d in search_dirs:
        if not d.is_dir():
            continue
        for crate in sorted(d.glob("*.crate")):
            if name_filter and crate.stem.lower() not in name_filter:
                continue
            crates.append(crate)
    return crates


def collect_serato_crate_mp3s(selected_names: Optional[list[str]] = None) -> list[Path]:
    """Return the de-duplicated list of .mp3 files referenced across the
    selected crates (or all crates if selected_names is None)."""
    crates = _find_crate_files(selected_names)
    if not crates:
        return []
    seen: set[Path] = set()
    out: list[Path] = []
    for crate in crates:
        for p in _parse_crate_paths(crate):
            if p.suffix.lower() != ".mp3":
                continue
            if p in seen:
                continue
            seen.add(p)
            if p.is_file():
                out.append(p)
    return out


def retag_files(
    mp3s: list[Path],
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    """Retag a specific list of mp3 files (no directory walk)."""
    stats: dict = {
        "total": len(mp3s),
        "ok": 0,
        "failed": 0,
        "errors": [],
    }
    for i, mp3 in enumerate(mp3s, 1):
        if progress_cb:
            progress_cb(i, len(mp3s), mp3)
        ok, msg = retag_file(mp3, dry_run=dry_run)
        if ok:
            stats["ok"] += 1
        else:
            stats["failed"] += 1
            stats["errors"].append((str(mp3), msg))
    return stats


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
        "--serato-crates",
        nargs="?",
        const="",
        default=None,
        help=(
            "Instead of walking a folder, retag only files that appear in your "
            "Serato subcrates (~/Music/_Serato_/Subcrates/*.crate and any "
            "external volumes). Pass a comma-separated list of crate names to "
            "limit the scope, e.g. --serato-crates 'Hip Hop,Bangers'. Pass "
            "the flag alone to retag files in ALL crates."
        ),
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

    def progress(i: int, n: int, p: Path) -> None:
        if i == n or i % max(1, n // 100) == 0:
            print(f"\r  [{i}/{n}] {p.name[:60]:<60}", end="", flush=True)

    # ---------- Serato-crate mode ----------
    if args.serato_crates is not None:
        names = (
            [s.strip() for s in args.serato_crates.split(",") if s.strip()]
            if args.serato_crates else None
        )
        crates = _find_crate_files(names)
        if not crates:
            print(
                "No Serato crates found. Checked ~/Music/_Serato_/Subcrates "
                "and /Volumes/*/_Serato_/Subcrates. "
                + (f"(Filter: {names})" if names else "")
            )
            return 1
        print(f"Found {len(crates)} crate(s):")
        for c in crates[:20]:
            print(f"  • {c.stem}  ({c})")
        if len(crates) > 20:
            print(f"  …and {len(crates) - 20} more")

        mp3s = collect_serato_crate_mp3s(names)
        if not mp3s:
            print("No .mp3 files found in those crates (or paths are stale / unreachable).")
            return 0
        print(f"\nFound {len(mp3s)} unique .mp3 file(s) in the crate(s).")
        if args.dry_run:
            print("DRY RUN — no files will change")
        print("Re-tagging with ID3v2.3 + Xing header (stream copy, no quality loss)...")
        stats = retag_files(mp3s, dry_run=args.dry_run, progress_cb=progress)
        print()
        print(f"✓ Re-tagged: {stats['ok']} / {stats['total']}")
        if stats["failed"]:
            print(f"✗ Failed:    {stats['failed']}")
            for path, err in stats["errors"][:10]:
                print(f"    {Path(path).name}: {err}")
            if len(stats["errors"]) > 10:
                print(f"    …and {len(stats['errors']) - 10} more (see log)")

        # Delete .serato sidecars for the folders those files live in.
        if args.delete_serato:
            parent_dirs = {m.parent for m in mp3s}
            removed = 0
            for d in parent_dirs:
                for p in d.rglob(".serato"):
                    if not p.is_dir():
                        continue
                    removed += 1
                    if not args.dry_run:
                        shutil.rmtree(p, ignore_errors=True)
            verb = "would delete" if args.dry_run else "deleted"
            print(
                f"{verb} {removed} .serato sidecar folder(s) in the "
                "folders those files live in — Serato will re-analyze on next scan."
            )
        else:
            print(
                "\nNote: your Serato library's analysis cache is in the main "
                "Serato database (~/Music/_Serato_/) and in per-folder .serato "
                "sidecars. In Serato: right-click the crate's tracks → 'Rescan "
                "ID3 tags' to pick up the new headers; the overview waveform "
                "re-renders on next play (or right-click → 'Set / Edit Grid' → "
                "it re-analyzes as part of that)."
            )
        return 0 if stats["failed"] == 0 else 2

    # ---------- Folder-walk mode (original) ----------
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
