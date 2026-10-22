"""Telegram bot frontend for musicdl.

Env vars (all read at startup):
  TELEGRAM_BOT_TOKEN         -- from @BotFather (required)
  TELEGRAM_ALLOWED_USER_IDS  -- comma-separated numeric Telegram user IDs
                                (required; the bot ignores everyone else)
  MUSICDL_OUTPUT_DIR         -- override the download root (optional)
  MUSICDL_SHELL_ENABLED      -- "1" to enable the /shell command (default off)

Chat protocol:
  <URL>                 -> download that URL (single or auto-detected playlist)
  !set <URL>            -> parse tracklist and download each track separately
  !playlist <URL>       -> force playlist mode
  !search <query>       -> free-text search
  --variant "..." can be appended to any of the above
Commands:
  /start /help          -> usage
  /whoami               -> your Telegram user id (useful when setting allowlist)
  /git pull             -> git pull in the repo root
  /shell <cmd>          -> run a shell command (requires MUSICDL_SHELL_ENABLED=1)
  /restart              -> exit(0); relies on your process manager to restart
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import wraps
from html import escape as html_escape
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote_plus

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .downloader import (
    download_playlist_entries,
    download_search,
    download_search_with_fallbacks,
    download_url,
    extract_playlist_entries,
    get_video_metadata,
    is_spotify_url,
)
from .notify import notify
from .organizer import playlist_dir, set_dir, single_dir
from .spotify import download_spotify
from . import state as _state
from .tracklist import parse_tracklist_from_info
from .tracklist_web import discover_tracklist_from_web

log = logging.getLogger("musicdl.bot")

REPO_ROOT = Path(__file__).resolve().parent.parent

URL_RE = re.compile(r"https?://\S+")

# Any video at least this long is treated as a probable DJ set / mix / show,
# and we try to parse its tracklist automatically before downloading.
_LONG_VIDEO_THRESHOLD_SEC = 20 * 60


# ------------- cancellation + progress state (module-wide) -------------
#
# One shared cancel flag per bot — /cancel sets it, every active worker
# checks it between tracks and at every yt-dlp progress tick, and the flag
# is cleared automatically when the next job starts. Simple and sufficient
# for a single-user bot.


class UserCancelled(Exception):
    """Raised from the yt-dlp progress hook when the user sent /cancel."""


_CANCEL_EVENT = threading.Event()


def _reset_cancel() -> None:
    _CANCEL_EVENT.clear()


def _is_cancelled() -> bool:
    return _CANCEL_EVENT.is_set()


@dataclass
class ActiveJob:
    """What the bot is currently doing (one at a time, by design)."""
    kind: str                              # 'url' | 'set' | 'search' | …
    label: str                             # human-readable description
    started_at: float = field(default_factory=time.monotonic)
    total: int = 0                         # tracks in set/playlist, 0 for single
    done: int = 0
    failed: int = 0
    current_track: str = ""


_ACTIVE_JOB: Optional[ActiveJob] = None
_ACTIVE_LOCK = threading.Lock()


def _set_active(job: Optional[ActiveJob]) -> None:
    global _ACTIVE_JOB
    with _ACTIVE_LOCK:
        _ACTIVE_JOB = job


def _update_active(**fields) -> None:
    with _ACTIVE_LOCK:
        if _ACTIVE_JOB is None:
            return
        for k, v in fields.items():
            setattr(_ACTIVE_JOB, k, v)


def _get_active() -> Optional[ActiveJob]:
    with _ACTIVE_LOCK:
        return _ACTIVE_JOB


# Hook the GUI app registers so /restart stops+starts the in-thread bot
# instead of killing the Python process (which would also close the window).
# Terminal `musicdl-bot` leaves this None → /restart falls back to execv.
RESTART_HOOK: Optional[Callable[[], None]] = None


def _format_bar(fraction: float, width: int = 20) -> str:
    """Unicode progress bar — e.g. [███████░░░░░░░]."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _human_size(n: int | float | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _human_time(sec: int | float | None) -> str:
    if not sec or sec < 0:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    return f"{sec // 60}m{sec % 60:02d}s"


class ProgressReporter:
    """Edits one Telegram message with a live progress bar every ~2 seconds.

    Thread-safe: yt-dlp calls on_progress from a worker thread; we marshal
    the actual Telegram edits onto the bot's asyncio loop via
    run_coroutine_threadsafe. Edits are rate-limited and failures (Telegram
    429 / message-not-modified) are swallowed — progress is nice-to-have.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, chat, header: str) -> None:
        self.loop = loop
        self.chat = chat
        self.header = header
        self.msg = None
        self._last_edit = 0.0
        self._lock = threading.Lock()
        self._done = False

    async def start(self) -> None:
        self.msg = await self.chat.send_message(f"{self.header}\n⏳ starting…")

    def _render(self, d: dict) -> str:
        status = d.get("status")
        if status == "finished":
            return f"{self.header}\n✅ downloaded, converting to MP3…"
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        speed = d.get("speed") or 0
        eta = d.get("eta") or 0
        if total <= 0:
            return (
                f"{self.header}\n"
                f"⏳ {_human_size(done)} ({_human_size(speed)}/s)"
            )
        pct = done / total
        return (
            f"{self.header}\n"
            f"`{_format_bar(pct)}` {pct * 100:5.1f}%\n"
            f"{_human_size(done)} / {_human_size(total)} · "
            f"{_human_size(speed)}/s · ETA {_human_time(eta)}"
        )

    def on_progress(self, d: dict) -> None:
        """yt-dlp hook — raises UserCancelled when /cancel was sent."""
        if _is_cancelled():
            raise UserCancelled("cancelled via /cancel")
        now = time.monotonic()
        with self._lock:
            # Status "finished" always draws (shows the convert step).
            if d.get("status") != "finished" and now - self._last_edit < 2.0:
                return
            self._last_edit = now
        text = self._render(d)
        if self.msg is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.msg.edit_text(text[:3800], parse_mode=ParseMode.MARKDOWN),
                self.loop,
            )
            # Don't block the worker thread waiting for the edit.
            fut.add_done_callback(lambda _f: None)
        except RuntimeError:
            pass  # loop stopped — bot shutting down

    async def finish(self, text: str) -> None:
        self._done = True
        if self.msg is None:
            return
        try:
            await self.msg.edit_text(text[:3800])
        except Exception:
            pass


def _make_cancellable_hook(reporter: Optional[ProgressReporter]) -> Callable[[dict], None]:
    """Compose a yt-dlp progress_hook that also enforces the /cancel flag."""
    def hook(d: dict) -> None:
        if _is_cancelled():
            raise UserCancelled("cancelled via /cancel")
        if reporter is not None:
            reporter.on_progress(d)
    return hook


def _parse_allowlist(raw: str | None) -> set[int]:
    if not raw:
        return set()
    ids: set[int] = set()
    for part in raw.split(","):
        p = part.strip()
        if p.isdigit():
            ids.add(int(p))
    return ids


ALLOWED_USER_IDS = _parse_allowlist(os.environ.get("TELEGRAM_ALLOWED_USER_IDS"))
SHELL_ENABLED = os.environ.get("MUSICDL_SHELL_ENABLED") == "1"


def restricted(handler: Callable) -> Callable:
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ALLOWED_USER_IDS:
            uid = user.id if user else "unknown"
            log.warning("Rejected message from user %s", uid)
            if update.effective_chat:
                await update.effective_chat.send_message(
                    f"Not authorised. Your Telegram user id is {uid}. "
                    "Ask the bot admin to add it to TELEGRAM_ALLOWED_USER_IDS."
                )
            return
        return await handler(update, context)

    return wrapper


_NUMBERED_TRACK = re.compile(r"\s(?=\d{1,3}\s*[\.\)]\s+)")


def _split_tracklist_payload(payload: str) -> tuple[str, str]:
    """Return (set_name, tracks_body) from a !tracklist message body.

    Accepts either:
      - Multi-line: first line is set name, remaining lines are tracks.
      - One line with numbered tracks: 'Ibiza Set 1. A - B 2. C - D 3. E - F'
        The prefix before the first '1.' becomes the set name.
    """
    lines = [ln.strip() for ln in payload.splitlines() if ln.strip()]
    if len(lines) > 1:
        return lines[0], "\n".join(lines[1:])

    single = lines[0] if lines else ""
    if not single:
        return "", ""

    # Split before " N. " tokens. First segment is the set name.
    parts = _NUMBERED_TRACK.split(single)
    if len(parts) >= 2:
        set_name = parts[0].strip()
        # Strip the leading "N." from each track segment.
        tracks = [
            re.sub(r"^\d{1,3}\s*[\.\)]\s+", "", p).strip() for p in parts[1:]
        ]
        return set_name, "\n".join(t for t in tracks if t)

    # No numbered pattern — nothing usable.
    return "", ""


def _extract_variant(text: str) -> tuple[str, str | None]:
    """Pull --variant "..." out of message text; return (remaining, variant)."""
    m = re.search(r'--variant\s+(?:"([^"]+)"|(\S+))', text)
    if not m:
        return text, None
    variant = m.group(1) or m.group(2)
    remaining = (text[: m.start()] + text[m.end():]).strip()
    return remaining, variant


async def _reply(update: Update, msg: str) -> None:
    if update.effective_chat:
        # 4096 is Telegram's per-message cap.
        for chunk_start in range(0, len(msg), 3800):
            await update.effective_chat.send_message(msg[chunk_start : chunk_start + 3800])


def _run_in_thread(fn, *args, **kwargs):
    return asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


# ---------- command handlers ----------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(
        update,
        "musicdl bot ready.\n\n"
        "Just send a URL — I'll figure out what to do:\n"
        "  • Track     → download as 320 kbps MP3\n"
        "  • Playlist  → download each track in order\n"
        "  • Long video (>20 min) → treated as a DJ set: auto-parse tracklist "
        "(description → 1001tracklists) and download each track; falls back to "
        "the full video if no tracklist is found.\n\n"
        "Explicit prefixes (only needed to override the automatic behavior):\n"
        "  !set <url>       — force set-parse mode (never falls back to full video)\n"
        "  !full <url>      — download the whole video as one MP3, no tracklist parse\n"
        "  !playlist <url>  — force playlist mode\n"
        "  !search <query>  — free-text search\n"
        "  !tracklist <name>\\n<lines>  — download a pasted tracklist\n\n"
        "Append --variant \"Extended Mix\" to steer version selection.\n\n"
        "Live control:\n"
        "  /status  — what's downloading right now\n"
        "  /cancel  — stop the current download / set\n"
        "  /health  — diagnostic: python, yt-dlp, ffmpeg, disk, cookies, queue\n"
        "  /restart — restart the bot cleanly\n"
        "  /queue   — list unfinished jobs across restarts\n\n"
        "Admin: /whoami /git pull"
        + (" /shell" if SHELL_ENABLED else ""),
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id if user else "unknown"
    name = user.full_name if user else ""
    await _reply(update, f"user_id={uid} name={name}")


@restricted
async def cmd_git(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or args[0] != "pull":
        await _reply(update, "Only /git pull is supported.")
        return
    proc = await _run_in_thread(
        subprocess.run,
        ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    body = (proc.stdout or "") + (proc.stderr or "")
    await _reply(update, f"exit={proc.returncode}\n{body.strip() or '(no output)'}")


@restricted
async def cmd_shell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not SHELL_ENABLED:
        await _reply(update, "/shell disabled. Set MUSICDL_SHELL_ENABLED=1 to enable.")
        return
    raw = update.effective_message.text or ""
    _, _, cmd = raw.partition(" ")
    cmd = cmd.strip()
    if not cmd:
        await _reply(update, "Usage: /shell <command>")
        return
    log.warning("Running /shell from %s: %s", update.effective_user.id, cmd)
    proc = await _run_in_thread(
        subprocess.run,
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    body = (proc.stdout or "") + (proc.stderr or "")
    await _reply(update, f"exit={proc.returncode}\n{body.strip() or '(no output)'}")


@restricted
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart the bot in-place.

    In the GUI app a RESTART_HOOK is registered that stops+starts the bot
    thread without killing the Python process (which would also close the
    window). In the terminal `musicdl-bot` the hook is None and we fall
    back to re-execing the current process.
    """
    if _get_active() is not None:
        await _reply(
            update,
            "⚠️ A download is in flight. Send /cancel first, or /restart again "
            "to force.",
        )
        # Allow a second /restart within 30 s to force through.
        global _RESTART_CONFIRMED_AT
        if time.monotonic() - _RESTART_CONFIRMED_AT < 30:
            pass  # user is forcing
        else:
            _RESTART_CONFIRMED_AT = time.monotonic()
            return

    await _reply(update, "🔄 Restarting bot…")
    log.info("Restart requested by %s", update.effective_user.id)
    _reset_cancel()
    _set_active(None)
    await asyncio.sleep(1)  # flush the message

    if RESTART_HOOK is not None:
        # GUI mode — hop off this loop before the hook stops it.
        asyncio.get_event_loop().call_later(0.1, RESTART_HOOK)
        return
    # Terminal mode — re-exec in place so systemd/launchd isn't required.
    try:
        os.execv(sys.executable, [sys.executable, "-m", "musicdl.bot"])
    except OSError as e:
        log.warning("execv failed (%s), falling back to exit", e)
        sys.exit(0)


_RESTART_CONFIRMED_AT: float = 0.0


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the active download / set."""
    job = _get_active()
    if job is None:
        await _reply(update, "Nothing to cancel — no active download.")
        return
    _CANCEL_EVENT.set()
    await _reply(
        update,
        f"⛔ Cancelling *{job.label}*…\nCurrent track aborts at the next "
        f"byte; set stops after this track.",
    )


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show what the bot is currently doing (0 or 1 active job)."""
    job = _get_active()
    if job is None:
        await _reply(update, "💤 Idle. No active download.")
        return
    elapsed = time.monotonic() - job.started_at
    lines = [
        f"🎧 Active: *{job.label}*",
        f"Kind: `{job.kind}` · running {_human_time(elapsed)}",
    ]
    if job.total > 1:
        pct = job.done / job.total
        lines.append(
            f"`{_format_bar(pct)}` {job.done}/{job.total}"
            + (f" · {job.failed} failed" if job.failed else "")
        )
        if job.current_track:
            lines.append(f"Now: {job.current_track}")
    lines.append("")
    lines.append("Send /cancel to stop.")
    await _reply(update, "\n".join(lines))


@restricted
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Self-check so the user can see 'is the bot actually working?' at a glance.

    Checks: python version, musicdl install path, yt-dlp/ffmpeg presence +
    versions, output directory writability, cookies file validity, pending
    queue depth, active job, uptime.
    """
    import importlib.metadata
    import shutil

    out = ["🩺 *musicdl health check*", ""]

    # ---- Python / install identity
    import musicdl
    out.append(f"Python: `{sys.executable.split('/')[-1]}` {sys.version.split()[0]}")
    out.append(f"musicdl path: `{Path(musicdl.__file__).parent}`")

    # ---- yt-dlp
    try:
        yt_version = importlib.metadata.version("yt-dlp")
        out.append(f"✅ yt-dlp {yt_version}")
    except Exception as e:  # noqa: BLE001
        out.append(f"❌ yt-dlp missing: {e}")

    # ---- ffmpeg
    try:
        probe = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        first = (probe.stdout or probe.stderr).splitlines()[0] if probe.returncode == 0 else "?"
        if probe.returncode == 0:
            out.append(f"✅ ffmpeg: {first[:80]}")
        else:
            out.append(f"❌ ffmpeg returned exit={probe.returncode}")
    except FileNotFoundError:
        out.append("❌ ffmpeg not on PATH — downloads will fail to transcode")
    except Exception as e:  # noqa: BLE001
        out.append(f"⚠️ ffmpeg check failed: {e}")

    # ---- output dir
    out_dir = Path(os.environ.get("MUSICDL_OUTPUT_DIR") or Path.home() / "Desktop" / "MusicDownloads")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".musicdl-write-probe"
        probe.write_text("ok")
        probe.unlink()
        free_mb = shutil.disk_usage(out_dir).free / 1024 / 1024
        out.append(f"✅ output dir writable: `{out_dir}` ({free_mb:,.0f} MB free)")
    except Exception as e:  # noqa: BLE001
        out.append(f"❌ output dir not writable: `{out_dir}` ({e})")

    # ---- cookies
    cookies = os.environ.get("MUSICDL_COOKIES_FILE")
    if cookies:
        p = Path(cookies)
        if p.is_file():
            age = int((time.time() - p.stat().st_mtime) / 86400)
            out.append(f"✅ cookies.txt: `{p.name}` (age {age}d)")
        else:
            out.append(f"❌ cookies file not found: `{cookies}`")
    else:
        out.append("ℹ️  no cookies file set (unauthenticated downloads only)")

    # ---- state (queue, active)
    pending = _state.peek_pending()
    if pending:
        out.append(f"⚠️ {len(pending)} job(s) in persistent queue — send /queue to see")
    else:
        out.append("✅ no pending jobs in queue")

    job = _get_active()
    if job is None:
        out.append("💤 idle (no active download)")
    else:
        out.append(f"🎧 active: {job.label} ({job.done}/{job.total})")

    uptime = time.monotonic() - _BOT_STARTED_AT
    out.append(f"⏱ bot uptime: {_human_time(uptime)}")

    await _reply(update, "\n".join(out))


_BOT_STARTED_AT: float = time.monotonic()


# ---------- download handlers ----------


async def _try_find_tracklist(update, info: dict):
    """Return (tracks, source_url) — description first, then web fallback."""
    tracks = parse_tracklist_from_info(info)
    if tracks:
        return tracks, None
    await _reply(update, "Description had no tracklist — searching the web…")
    discovery = await _run_in_thread(discover_tracklist_from_web, info)
    return discovery.tracks, discovery.source_url


async def _download_as_full_video(update, url: str) -> None:
    out = single_dir(None)
    chat = update.effective_chat
    loop = asyncio.get_event_loop()
    reporter = ProgressReporter(loop, chat, f"⬇️ {url}")
    await reporter.start()
    hook = _make_cancellable_hook(reporter)
    _reset_cancel()
    _set_active(ActiveJob(kind="url", label=url, total=1))
    try:
        result = await _run_in_thread(
            download_url, url, out, None, None, progress_hook=hook
        )
        await reporter.finish(f"✅ Saved {result.filepath.name}")
        notify("musicdl", f"Saved {result.filepath.name}")
    except UserCancelled:
        await reporter.finish("⛔ Cancelled.")
        _reset_cancel()
    finally:
        _set_active(None)


async def _handle_url(update, url: str, variant: str | None, force_full: bool = False) -> None:
    output = None  # use default root
    if is_spotify_url(url):
        out = single_dir(output) if "/track/" in url else playlist_dir("Spotify", output)
        await _reply(update, f"Spotify -> spotdl into {out}")
        rc = await _run_in_thread(download_spotify, url, out)
        await _reply(update, f"spotdl finished (exit={rc})")
        return

    await _reply(update, f"Inspecting {url}")
    info = await _run_in_thread(get_video_metadata, url)
    is_playlist = info.get("_type") == "playlist" or bool(info.get("entries"))
    if is_playlist:
        name, entries = await _run_in_thread(extract_playlist_entries, url)
        out = playlist_dir(name, output)
        await _reply(update, f"Playlist {name!r}: {len(entries)} entries -> {out}")
        results = await _run_in_thread(download_playlist_entries, entries, out)
        await _reply(update, f"Downloaded {len(results)} tracks.")
        return

    duration = int(info.get("duration") or 0)
    if not force_full and duration >= _LONG_VIDEO_THRESHOLD_SEC:
        await _reply(
            update,
            f"Long video ({duration // 60}m) — treating as a DJ set and trying "
            "to parse its tracklist. (Send `!full <url>` if you want the whole "
            "thing as one MP3 instead.)",
        )
        # Re-probe with comments now that we've committed to set-parsing.
        info = await _run_in_thread(get_video_metadata, url, with_comments=True)
        tracks, source = await _try_find_tracklist(update, info)
        if tracks:
            if source:
                await _reply(update, f"Found {len(tracks)} tracks via {source}")
            name = info.get("title") or "set"
            out = set_dir(name, output)
            await _download_tracks_with_progress(update, tracks, out, label=name)
            return
        await _reply(
            update,
            "No tracklist found automatically — downloading the full video as "
            "one MP3 instead.",
        )

    await _download_as_full_video(update, url)


def _search_url(service: str, query: str) -> str:
    q = quote_plus(query)
    return {
        "youtube": f"https://www.youtube.com/results?search_query={q}",
        "soundcloud": f"https://soundcloud.com/search/sounds?q={q}",
        "beatport": f"https://www.beatport.com/search?q={q}",
        "bandcamp": f"https://bandcamp.com/search?q={q}",
        "traxsource": f"https://www.traxsource.com/search?term={q}",
        "1001tl": f"https://www.1001tracklists.com/search?q={q}",
    }[service]


def _explain_failure(msg: str) -> tuple[str, str]:
    """Return (short_reason, follow_up_hint) for a track download error."""
    m = msg.lower()
    if "requested version" in m and "not found" in m:
        return (
            "Requested version (remix/edit/bootleg/mix) isn't on YouTube or SoundCloud — only the original (or nothing) is available.",
            "Drop the version qualifier to grab the original, or check Beatport/Traxsource. "
            "For bootlegs, split into source tracks instead.",
        )
    if "all results" in m and "too long" in m:
        return (
            "Every candidate on YouTube and SoundCloud was longer than 12 min — track probably only exists inside longer DJ sets.",
            "If it's a bootleg/mashup, break it into source tracks. Beatport/Traxsource carry official standalone releases.",
        )
    if "no usable result" in m or ("no youtube matches" in m and "no soundcloud" in m):
        return (
            "No matches on YouTube or SoundCloud.",
            "Fix the spelling, or check Beatport/Bandcamp. Might be an obscure/unreleased ID.",
        )
    if "audio extraction produced no" in m or "extraction produced no" in m:
        return (
            "Top result had no extractable audio — likely age-restricted, DRM, or image-only.",
            "Append --variant \"<channel or version>\" to pick a different upload, or paste a direct URL.",
        )
    if "hit has no url" in m:
        return (
            "Search returned a result without a usable URL.",
            "Retry, or add --variant to steer the search.",
        )
    short = msg.split("\n")[0][:180]
    return (short, "Paste a direct URL for this track, or check Beatport/Bandcamp.")


def _chunk_by_lines(text: str, limit: int = 3800) -> list[str]:
    """Split text into chunks under `limit` chars without breaking lines."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        add = len(line) + 1
        if current and current_len + add > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add
        else:
            current.append(line)
            current_len += add
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _send_failure_report(update: Update, failed: list[tuple[Any, str]]) -> None:
    """Send a detailed per-track failure explanation with search links."""
    if not failed:
        return
    lines = [f"<b>{len(failed)} track(s) failed. Details:</b>", ""]
    for entry, err in failed:
        query = entry.query
        reason, hint = _explain_failure(err)
        yt = _search_url("youtube", query)
        sc = _search_url("soundcloud", query)
        bp = _search_url("beatport", query)
        bc = _search_url("bandcamp", query)
        tx = _search_url("traxsource", query)
        lines.append(f"<b>{entry.index}. {html_escape(query)}</b>")
        lines.append(f"  ↳ {html_escape(reason)}")
        if hint:
            lines.append(f"  ↳ {html_escape(hint)}")
        lines.append(
            f'  ↳ Search: <a href="{yt}">YouTube</a> · '
            f'<a href="{sc}">SoundCloud</a> · '
            f'<a href="{bp}">Beatport</a> · '
            f'<a href="{bc}">Bandcamp</a> · '
            f'<a href="{tx}">Traxsource</a>'
        )
        lines.append("")
    chat = update.effective_chat
    if not chat:
        return
    for chunk in _chunk_by_lines("\n".join(lines)):
        try:
            await chat.send_message(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("failure report send failed, falling back to plain: %s", e)
            await chat.send_message(chunk[:3800])


async def _download_tracks_with_progress(
    update: Update,
    tracks: list,
    out_dir: Path,
    label: str,
) -> None:
    """Download each track via search; edit a single status message as it goes.

    Rolling status includes a progress bar for the overall set + the current
    track name + per-track byte progress. /cancel breaks out between tracks
    AND aborts the in-flight yt-dlp fetch via the shared cancel event.

    On completion, sends a separate detailed failure report with per-track
    explanations and clickable search links if any tracks failed.
    """
    chat = update.effective_chat
    n = len(tracks)
    status = await chat.send_message(
        f"{label}: 0/{n}\nSaving to {out_dir}"
    )
    last_edit = 0.0
    ok = 0
    failed: list[tuple[Any, str]] = []
    current_pct: dict[str, Any] = {"frac": 0.0, "speed": 0, "eta": 0}

    loop = asyncio.get_event_loop()
    _reset_cancel()
    _set_active(ActiveJob(kind="set", label=label, total=n))

    async def edit(text: str, force: bool = False) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if not force and (now - last_edit) < 2.0:
            return
        try:
            await status.edit_text(text[:3800], parse_mode=ParseMode.MARKDOWN)
            last_edit = now
        except Exception:
            pass  # rate limit / race / message not modified — safe to ignore

    def _per_track_hook(d: dict) -> None:
        if _is_cancelled():
            raise UserCancelled("cancelled via /cancel")
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        if total > 0:
            current_pct["frac"] = done / total
        current_pct["speed"] = d.get("speed") or 0
        current_pct["eta"] = d.get("eta") or 0

    for i, entry in enumerate(tracks, 1):
        if _is_cancelled():
            await edit(f"⛔ {label}: cancelled at track {i} of {n}", force=True)
            _reset_cancel()
            _set_active(None)
            await _send_failure_report(update, failed)
            return
        current_pct["frac"] = 0.0
        overall = (i - 1) / n
        _update_active(done=i - 1, failed=len(failed), current_track=entry.query)
        header = (
            f"*{label}*\n"
            f"`{_format_bar(overall)}` {ok}/{n}"
            + (f" · {len(failed)} failed" if failed else "")
            + f"\nNow #{i}: {entry.query}"
        )
        await edit(header)
        try:
            await _run_in_thread(
                download_search_with_fallbacks,
                entry.artist,
                entry.title,
                entry.text,
                None,
                dest_dir=out_dir,
                playlist_index=entry.index,
                filename_hint=entry.filename,
                progress_hook=_per_track_hook,
            )
            ok += 1
        except UserCancelled:
            await edit(f"⛔ {label}: cancelled during track {i}", force=True)
            _reset_cancel()
            _set_active(None)
            await _send_failure_report(update, failed)
            return
        except Exception as e:
            failed.append((entry, str(e)))

    final = f"✅ *{label}*: {ok}/{n} done"
    if failed:
        final += f" · {len(failed)} failed (details below)"
    await edit(final, force=True)
    _set_active(None)
    await _send_failure_report(update, failed)
    notify(
        "musicdl",
        f"{label}: {ok}/{n} done"
        + (f" ({len(failed)} failed)" if failed else ""),
    )


async def _handle_set(update, url: str) -> None:
    """Explicit !set — refuse to fall back to a full-video download.

    Reports what was tried when nothing works so the user can paste
    a tracklist manually with !tracklist.
    """
    output = None
    await _reply(update, f"Parsing DJ set: {url}")
    info = await _run_in_thread(get_video_metadata, url, with_comments=True)
    tracks, source = await _try_find_tracklist(update, info)
    if not tracks:
        source_line = f"\n\nSaw this URL but couldn't parse it: {source}" if source else ""
        await _reply(
            update,
            "No tracklist found automatically.\n"
            f"{source_line}\n\n"
            "Paste it manually with:\n"
            "!tracklist Set Name\n"
            "Artist - Title\n"
            "Artist - Title\n"
            "...\n\n"
            "(Timestamps like `01:23 Artist - Title` also work.)",
        )
        return
    if source:
        await _reply(update, f"Found {len(tracks)} tracks via {source}")
    name = info.get("title") or "set"
    out = set_dir(name, output)
    await _download_tracks_with_progress(update, tracks, out, label=name)


async def _handle_playlist(update, url: str) -> None:
    output = None
    name, entries = await _run_in_thread(extract_playlist_entries, url)
    out = playlist_dir(name, output)
    await _reply(update, f"Playlist {name!r}: {len(entries)} entries -> {out}")
    results = await _run_in_thread(download_playlist_entries, entries, out)
    await _reply(update, f"Downloaded {len(results)} tracks.")


async def _handle_tracklist_text(update, name: str, text: str) -> None:
    """Download each track from a pasted tracklist (timestamped or plain lines)."""
    from .tracklist import parse_tracklist, TracklistEntry, _split_artist_title

    tracks = parse_tracklist(text)
    if not tracks:
        # Fallback: treat each non-empty line as "Artist - Title".
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
        await _reply(update, "No tracks recognized in that message.")
        return
    out = set_dir(name, None)
    await _download_tracks_with_progress(update, tracks, out, label=name)


async def _handle_search(update, query: str, variant: str | None) -> None:
    out = single_dir(None)
    chat = update.effective_chat
    header = f"🔎 {query}" + (f' [{variant}]' if variant else "")
    loop = asyncio.get_event_loop()
    reporter = ProgressReporter(loop, chat, header)
    await reporter.start()
    hook = _make_cancellable_hook(reporter)
    _reset_cancel()
    _set_active(ActiveJob(kind="search", label=query, total=1))
    try:
        result = await _run_in_thread(
            download_search,
            query,
            None,
            variant,
            dest_dir=out,
            playlist_index=None,
            filename_hint=None,
            progress_hook=hook,
        )
        await reporter.finish(f"✅ Saved {result.filepath.name}")
        notify("musicdl", f"Saved {result.filepath.name}")
    except UserCancelled:
        await reporter.finish("⛔ Cancelled.")
        _reset_cancel()
    finally:
        _set_active(None)


def _classify(text: str) -> tuple[str, str]:
    """Return (kind, payload) for logging/queue purposes only."""
    t = text.strip().lower()
    if t.startswith("!set"):
        return "set", text
    if t.startswith("!playlist"):
        return "playlist", text
    if t.startswith("!full"):
        return "full", text
    if t.startswith("!tracklist"):
        return "tracklist", text
    if t.startswith("!search"):
        return "search", text
    if URL_RE.search(text):
        return "url", text
    return "search", text


@restricted
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if update.effective_chat:
        await update.effective_chat.send_action(ChatAction.TYPING)

    text, variant = _extract_variant(text)

    # Persist the job before we start, so a crash mid-download leaves a
    # breadcrumb (the user can see what was in flight after a restart).
    kind, payload = _classify(text)
    job = _state.enqueue(
        kind,
        payload,
        extra={
            "chat_id": update.effective_chat.id if update.effective_chat else None,
            "user_id": update.effective_user.id if update.effective_user else None,
            "variant": variant,
        },
    )
    _state.mark_started(job.id)

    try:
        if text.lower().startswith("!set"):
            payload = text[4:].strip()
            m = URL_RE.search(payload)
            if not m:
                await _reply(update, "!set expects a URL.")
                return
            await _handle_set(update, m.group(0))
            return

        if text.lower().startswith("!playlist"):
            payload = text[9:].strip()
            m = URL_RE.search(payload)
            if not m:
                await _reply(update, "!playlist expects a URL.")
                return
            await _handle_playlist(update, m.group(0))
            return

        if text.lower().startswith("!full"):
            payload = text[5:].strip()
            m = URL_RE.search(payload)
            if not m:
                await _reply(update, "!full expects a URL.")
                return
            await _handle_url(update, m.group(0), variant, force_full=True)
            return

        if text.lower().startswith("!tracklist"):
            payload = text[10:].strip()
            set_name, body = _split_tracklist_payload(payload)
            if not set_name or not body:
                await _reply(
                    update,
                    "!tracklist expects a set name and at least one track.\n\n"
                    "Multi-line form:\n"
                    "!tracklist Ibiza Set\n"
                    "Artist - Title\n"
                    "Artist - Title\n\n"
                    "One-line form (numbered):\n"
                    "!tracklist Ibiza Set 1. Artist - Title 2. Artist - Title",
                )
                return
            await _handle_tracklist_text(update, set_name, body)
            return

        if text.lower().startswith("!search"):
            query = text[7:].strip()
            if not query:
                await _reply(update, "!search expects a query.")
                return
            await _handle_search(update, query, variant)
            return

        m = URL_RE.search(text)
        if m:
            await _handle_url(update, m.group(0), variant)
            return

        # Fallback: treat whole message as search query.
        await _handle_search(update, text, variant)

    except Exception as e:
        log.exception("Handler failed")
        await _reply(update, f"Error: {e!r}")
    finally:
        _state.mark_done(job.id)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/queue` → list pending/crashed-mid-flight jobs; `/queue clear` nukes them."""
    args = context.args or []
    if args and args[0] == "clear":
        n = _state.clear_queue()
        await _reply(update, f"Cleared {n} queued job(s).")
        return
    pending = _state.peek_pending()
    if not pending:
        await _reply(update, "Queue empty.")
        return
    lines = [f"{len(pending)} job(s) pending (unfinished across restarts):"]
    for j in pending[:30]:
        age = int(time.time() - j.enqueued_at)
        lines.append(f"  [{j.kind}] attempts={j.attempts} age={age}s  {j.payload[:80]}")
    if len(pending) > 30:
        lines.append(f"  …and {len(pending) - 30} more")
    lines.append("")
    lines.append("Resend what you still want; `/queue clear` to drop them all.")
    await _reply(update, "\n".join(lines))


async def _announce_pending_on_startup(app: Application) -> None:
    """If any jobs are in the queue from a prior session, DM the first
    allowed user so they know work was interrupted."""
    pending = _state.peek_pending()
    if not pending:
        return
    allowed = sorted(ALLOWED_USER_IDS)
    if not allowed:
        return
    target = allowed[0]
    lines = [
        f"⚠️ Bot restarted with {len(pending)} unfinished job(s) in the queue:",
        "",
    ]
    for j in pending[:10]:
        lines.append(f"  • [{j.kind}] {j.payload[:90]}")
    if len(pending) > 10:
        lines.append(f"  …and {len(pending) - 10} more")
    lines.append("")
    lines.append("These were interrupted mid-download. Resend what you still "
                 "want, or send `/queue clear` to drop them.")
    try:
        await app.bot.send_message(chat_id=target, text="\n".join(lines))
    except Exception as e:  # noqa: BLE001
        log.warning("couldn't DM queue warning to %s: %s", target, e)


def build_app() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var is required.")
    if not ALLOWED_USER_IDS:
        raise SystemExit(
            "TELEGRAM_ALLOWED_USER_IDS is empty. Add your numeric Telegram "
            "user id (see /whoami on any Telegram id bot) as a comma-separated "
            "list; the bot ignores everyone else."
        )
    app = Application.builder().post_init(_announce_pending_on_startup).token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("shell", cmd_shell))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


def _log_install_identity() -> None:
    """Make it obvious at startup which copy of musicdl is actually loaded,
    so the "I pulled new code but the bot still runs old behavior" class of
    bug can be diagnosed in one line of the log."""
    import musicdl
    from . import tracklist as _tl

    marker = "Title-Artist"  # bump this string whenever the format changes
    log.info("musicdl package loaded from: %s", Path(musicdl.__file__).parent)
    log.info("python executable: %s", sys.executable)
    log.info("FILENAME-FORMAT=%s (TracklistEntry.filename)", marker)
    # Sanity check: construct a known entry and log the actual output.
    probe = _tl.TracklistEntry(
        index=1, timestamp="00:00", seconds=0,
        text="Fisher - Losing It", artist="Fisher", title="Losing It",
    )
    log.info("sanity probe: artist='Fisher' title='Losing It' -> %r", probe.filename)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _log_install_identity()
    app = build_app()
    log.info("Bot starting. Allowlist: %s", sorted(ALLOWED_USER_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
