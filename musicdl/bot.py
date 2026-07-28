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
import time
from functools import wraps
from pathlib import Path
from typing import Callable

from telegram import Update
from telegram.constants import ChatAction
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
    download_url,
    extract_playlist_entries,
    get_video_metadata,
    is_spotify_url,
)
from .organizer import playlist_dir, set_dir, single_dir
from .spotify import download_spotify
from .tracklist import parse_tracklist_from_info

log = logging.getLogger("musicdl.bot")

REPO_ROOT = Path(__file__).resolve().parent.parent

URL_RE = re.compile(r"https?://\S+")


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
        "Send a URL to download it as 320 kbps MP3.\n"
        "Prefix with !set for a DJ set (parses tracklist).\n"
        "Prefix with !playlist to force playlist mode.\n"
        "Use !search <query> for free-text lookup.\n"
        "Use !tracklist <name>\\n<lines> to download a pasted tracklist.\n"
        "Append --variant \"Extended Mix\" to steer the version.\n\n"
        "Commands: /whoami /git pull /restart"
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
    await _reply(update, "Restarting (relying on process manager).")
    log.info("Exiting on /restart from %s", update.effective_user.id)
    # Give Telegram a moment to flush the message before dying.
    await asyncio.sleep(1)
    sys.exit(0)


# ---------- download handlers ----------


async def _handle_url(update, url: str, variant: str | None) -> None:
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

    out = single_dir(output)
    result = await _run_in_thread(
        download_url, url, out, None, None
    )
    await _reply(update, f"Saved {result.filepath.name}")


async def _download_tracks_with_progress(
    update: Update,
    tracks: list,
    out_dir: Path,
    label: str,
) -> None:
    """Download each track via search; edit a single status message as it goes."""
    chat = update.effective_chat
    n = len(tracks)
    status = await chat.send_message(
        f"{label}: 0/{n}\nSaving to {out_dir}"
    )
    last_edit = 0.0
    ok = 0
    failed: list[str] = []

    async def edit(text: str, force: bool = False) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if not force and (now - last_edit) < 2.0:
            return
        try:
            await status.edit_text(text[:3800])
            last_edit = now
        except Exception:
            pass  # rate limit / race — safe to ignore

    for i, entry in enumerate(tracks, 1):
        await edit(
            f"{label}: {i - 1}/{n}"
            + (f" (failed {len(failed)})" if failed else "")
            + f"\nNow: {entry.query}"
        )
        try:
            await _run_in_thread(
                download_search,
                entry.title,
                entry.artist,
                None,
                dest_dir=out_dir,
                playlist_index=entry.index,
                filename_hint=entry.query,
            )
            ok += 1
        except Exception as e:
            failed.append(f"[{entry.index}] {entry.query}: {e}")

    final = f"{label}: {ok}/{n} done"
    if failed:
        preview = "\n".join(failed[:10])
        more = f"\n… and {len(failed) - 10} more" if len(failed) > 10 else ""
        final += f"\nFailed ({len(failed)}):\n{preview}{more}"
    await edit(final, force=True)


async def _handle_set(update, url: str) -> None:
    output = None
    await _reply(update, f"Parsing DJ set: {url}")
    info = await _run_in_thread(get_video_metadata, url)
    tracks = parse_tracklist_from_info(info)
    if not tracks:
        await _reply(
            update,
            "No tracklist found in description/comments.\n\n"
            "Send it manually with:\n"
            "!tracklist Set Name\n"
            "Artist - Title\n"
            "Artist - Title\n"
            "...\n\n"
            "(Timestamps like `01:23 Artist - Title` also work.)",
        )
        return
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
    await _reply(update, f"Searching: {query}" + (f' [{variant}]' if variant else ""))
    result = await _run_in_thread(
        download_search,
        query,
        None,
        variant,
        dest_dir=out,
        playlist_index=None,
        filename_hint=None,
    )
    await _reply(update, f"Saved {result.filepath.name}")


@restricted
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if update.effective_chat:
        await update.effective_chat.send_action(ChatAction.TYPING)

    text, variant = _extract_variant(text)

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
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("shell", cmd_shell))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = build_app()
    log.info("Bot starting. Allowlist: %s", sorted(ALLOWED_USER_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
