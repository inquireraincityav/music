"""Bot lifecycle for the desktop app.

The Telegram bot runs on its own asyncio loop in a background thread so the
Tkinter main loop isn't blocked. Start/stop is thread-safe; the GUI never
touches the bot's internals directly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from telegram import Update

log = logging.getLogger("musicdl.app.bot_manager")


class BotManager:
    """Owns a python-telegram-bot Application running in a background thread."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._app = None  # telegram.ext.Application
        self._started_event = threading.Event()
        self._error: Optional[Exception] = None

    # ---- public ----

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, env: dict[str, str]) -> None:
        """Start the bot with the given env vars applied to os.environ."""
        if self.is_running():
            return
        for k, v in env.items():
            os.environ[k] = v
        self._error = None
        self._started_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Give the loop a moment to fail fast on bad token, etc.
        self._started_event.wait(timeout=8)

    def error(self) -> Optional[Exception]:
        return self._error

    def stop(self, timeout: float = 10.0) -> None:
        if not self.is_running():
            self._thread = None
            self._loop = None
            self._app = None
            return
        if self._loop and self._app:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                fut.result(timeout=timeout)
            except Exception as e:
                log.warning("shutdown coroutine raised: %s", e)
        # Stop the loop and wait for the thread to exit.
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self._app = None

    # ---- internal ----

    def _run(self) -> None:
        # Fresh event loop for this thread.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            # Import here so env changes take effect for this run.
            from .bot import build_app

            app = build_app()
            self._app = app
            loop.run_until_complete(self._async_start(app))
            self._started_event.set()
            loop.run_forever()
        except Exception as e:
            log.exception("bot thread crashed")
            self._error = e
            self._started_event.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _async_start(self, app) -> None:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def _shutdown(self) -> None:
        try:
            if self._app and self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            if self._app and self._app.running:
                await self._app.stop()
            if self._app:
                await self._app.shutdown()
        except Exception as e:
            log.warning("bot shutdown raised: %s", e)
