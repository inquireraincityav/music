"""Persistent state for musicdl: download queue + completed-track registry.

Both live in the platform's app-support dir so they survive restarts and
crashes. Resumability + duplicate avoidance are built on this.

  <app_support>/state/queue.json      pending work (list of enqueued jobs)
  <app_support>/state/registry.json   {dest_path: {duration, mtime}} index of
                                      successful downloads for skip-if-exists.

Files are rewritten atomically (tmp + rename) so a crash mid-write doesn't
leave corrupted JSON.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .app_config import app_support_dir

log = logging.getLogger("musicdl.state")


def state_dir() -> Path:
    d = app_support_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_path() -> Path:
    return state_dir() / "queue.json"


def registry_path() -> Path:
    return state_dir() / "registry.json"


# ---------- registry (skip-if-already-downloaded) ----------

_registry_lock = threading.Lock()


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("corrupt state file %s — ignoring", path)
        return default


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_download(filepath: Path, duration: Optional[float]) -> None:
    """Record a successful download so later runs can skip it."""
    with _registry_lock:
        reg = _load_json(registry_path(), {})
        reg[str(filepath)] = {
            "duration": float(duration) if duration else None,
            "mtime": filepath.stat().st_mtime if filepath.exists() else time.time(),
        }
        _atomic_write_json(registry_path(), reg)


def lookup_registered(filepath: Path) -> Optional[dict]:
    """Return registry entry for filepath, or None."""
    reg = _load_json(registry_path(), {})
    return reg.get(str(filepath))


def forget_download(filepath: Path) -> None:
    with _registry_lock:
        reg = _load_json(registry_path(), {})
        if reg.pop(str(filepath), None) is not None:
            _atomic_write_json(registry_path(), reg)


# ---------- queue (persistent URL backlog) ----------


@dataclass
class QueueJob:
    """One unit of work queued by the bot / app."""
    id: str
    kind: str          # 'url' | 'set' | 'playlist' | 'search' | 'tracklist' | 'full'
    payload: str       # URL or query
    extra: dict = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)
    attempts: int = 0


_queue_lock = threading.Lock()


def _load_queue() -> list[dict]:
    data = _load_json(queue_path(), [])
    return data if isinstance(data, list) else []


def _save_queue(items: list[dict]) -> None:
    _atomic_write_json(queue_path(), items)


def enqueue(kind: str, payload: str, extra: dict | None = None) -> QueueJob:
    job = QueueJob(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        payload=payload,
        extra=extra or {},
    )
    with _queue_lock:
        q = _load_queue()
        q.append(asdict(job))
        _save_queue(q)
    log.info("enqueued %s %s (id=%s, backlog=%d)", kind, payload[:60], job.id, len(q))
    return job


def peek_pending() -> list[QueueJob]:
    """Snapshot of pending jobs, oldest first."""
    with _queue_lock:
        return [QueueJob(**j) for j in _load_queue()]


def mark_started(job_id: str) -> None:
    with _queue_lock:
        q = _load_queue()
        for j in q:
            if j.get("id") == job_id:
                j["attempts"] = int(j.get("attempts") or 0) + 1
                break
        _save_queue(q)


def mark_done(job_id: str) -> None:
    """Remove a finished job from the queue."""
    with _queue_lock:
        q = [j for j in _load_queue() if j.get("id") != job_id]
        _save_queue(q)


def clear_queue() -> int:
    """Nuke the queue (user action from UI). Returns count cleared."""
    with _queue_lock:
        q = _load_queue()
        _save_queue([])
        return len(q)
