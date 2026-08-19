from __future__ import annotations

from pathlib import Path

from .config import DEFAULT_OUTPUT_DIR, safe_filename


def output_root(root: Path | None = None) -> Path:
    root = Path(root) if root else DEFAULT_OUTPUT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def single_dir(root: Path | None = None) -> Path:
    d = output_root(root) / "Singles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def playlist_dir(playlist_name: str, root: Path | None = None) -> Path:
    d = output_root(root) / "Playlists" / safe_filename(playlist_name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_dir(set_name: str, root: Path | None = None) -> Path:
    d = output_root(root) / "Sets" / safe_filename(set_name)
    d.mkdir(parents=True, exist_ok=True)
    return d
