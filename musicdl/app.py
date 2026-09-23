"""musicdl desktop app — Tkinter GUI that runs the Telegram bot in-process.

Cross-platform (macOS + Windows + Linux). No terminal, no env vars: config
is stored in the platform-appropriate app-support directory and edited via
a simple Settings dialog.

Run in dev with:   musicdl-app     (installed by pyproject.toml entry point)
Or as a module:    python -m musicdl.app
Or as a bundled .app / .exe once PyInstaller has built it.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .app_bot_manager import BotManager
from .app_config import (
    AppConfig,
    app_support_dir,
    config_path,
    default_output_dir,
    load_config,
    save_config,
)

log = logging.getLogger("musicdl.app")

APP_TITLE = "musicdl"


# ---------- utilities ----------


def open_folder(path: str) -> None:
    """Reveal `path` in the platform's file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror(APP_TITLE, f"Couldn't open folder:\n{e}")


# ---------- settings dialog ----------


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, cfg: AppConfig) -> None:
        super().__init__(parent)
        self.title(f"{APP_TITLE} — settings")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.result: AppConfig | None = None

        pad = {"padx": 12, "pady": 6}

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, **pad)

        ttk.Label(frame, text="Telegram bot token").grid(row=0, column=0, sticky="w", **pad)
        self.token_var = tk.StringVar(value=cfg.telegram_bot_token)
        token_entry = ttk.Entry(frame, textvariable=self.token_var, width=52, show="•")
        token_entry.grid(row=0, column=1, sticky="ew", **pad)

        ttk.Label(frame, text="Allowed Telegram user id(s)\n(comma-separated)").grid(row=1, column=0, sticky="w", **pad)
        self.ids_var = tk.StringVar(
            value=", ".join(str(u) for u in cfg.telegram_allowed_user_ids)
        )
        ttk.Entry(frame, textvariable=self.ids_var, width=52).grid(row=1, column=1, sticky="ew", **pad)

        ttk.Label(frame, text="Download folder").grid(row=2, column=0, sticky="w", **pad)
        out_row = ttk.Frame(frame)
        out_row.grid(row=2, column=1, sticky="ew", **pad)
        self.out_var = tk.StringVar(
            value=cfg.output_dir or str(default_output_dir())
        )
        ttk.Entry(out_row, textvariable=self.out_var, width=42).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Choose…", command=self._pick_folder).pack(side="left", padx=(6, 0))

        ttk.Label(frame, text="Cookies file (optional)\n(for paid DJ pools)").grid(row=3, column=0, sticky="w", **pad)
        ck_row = ttk.Frame(frame)
        ck_row.grid(row=3, column=1, sticky="ew", **pad)
        self.ck_var = tk.StringVar(value=cfg.cookies_file)
        ttk.Entry(ck_row, textvariable=self.ck_var, width=42).pack(side="left", fill="x", expand=True)
        ttk.Button(ck_row, text="Choose…", command=self._pick_cookies).pack(side="left", padx=(6, 0))

        self.autostart_var = tk.BooleanVar(value=cfg.auto_start_bot)
        ttk.Checkbutton(
            frame,
            text="Start the bot automatically when this app opens",
            variable=self.autostart_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", **pad)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=(0, 6))

        token_entry.focus_set()

    def _pick_folder(self) -> None:
        selected = filedialog.askdirectory(
            title="Choose download folder",
            initialdir=self.out_var.get() or str(default_output_dir()),
        )
        if selected:
            self.out_var.set(selected)

    def _pick_cookies(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose cookies.txt",
            filetypes=[("Netscape cookies", "*.txt"), ("All files", "*.*")],
        )
        if selected:
            self.ck_var.set(selected)

    def _save(self) -> None:
        ids: list[int] = []
        for chunk in self.ids_var.get().split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError:
                messagebox.showerror(
                    APP_TITLE,
                    f"'{chunk}' is not a numeric Telegram user id.",
                    parent=self,
                )
                return
        cfg = AppConfig(
            telegram_bot_token=self.token_var.get().strip(),
            telegram_allowed_user_ids=ids,
            output_dir=self.out_var.get().strip(),
            cookies_file=self.ck_var.get().strip(),
            auto_start_bot=self.autostart_var.get(),
        )
        save_config(cfg)
        self.result = cfg
        self.destroy()


# ---------- main window ----------


class MainWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("460x230")
        self.minsize(420, 220)

        self.cfg = load_config()
        self.bot = BotManager()

        style = ttk.Style(self)
        try:
            style.theme_use("aqua" if sys.platform == "darwin" else "vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass

        # Menu bar
        menubar = tk.Menu(self)
        appmenu = tk.Menu(menubar, tearoff=False)
        appmenu.add_command(label="Settings…", command=self._open_settings, accelerator="⌘,")
        appmenu.add_separator()
        appmenu.add_command(label="Open downloads folder", command=self._open_downloads)
        appmenu.add_command(label="Open config folder", command=self._open_config_dir)
        appmenu.add_separator()
        appmenu.add_command(label="Quit", command=self._on_quit, accelerator="⌘Q")
        menubar.add_cascade(label="File", menu=appmenu)
        self.config(menu=menubar)
        self.bind_all("<Command-comma>", lambda _e: self._open_settings())
        self.bind_all("<Control-comma>", lambda _e: self._open_settings())

        # Body
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="● Bot: not started")
        self.status_lbl = ttk.Label(body, textvariable=self.status_var, font=("", 14))
        self.status_lbl.pack(anchor="w")

        self.path_var = tk.StringVar()
        ttk.Label(body, textvariable=self.path_var, foreground="#666").pack(
            anchor="w", pady=(4, 12)
        )

        btnrow = ttk.Frame(body)
        btnrow.pack(fill="x")
        self.toggle_btn = ttk.Button(btnrow, text="Start bot", command=self._toggle_bot)
        self.toggle_btn.pack(side="left")
        ttk.Button(btnrow, text="Settings…", command=self._open_settings).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btnrow, text="Open downloads", command=self._open_downloads).pack(
            side="left", padx=(8, 0)
        )

        self.info_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.info_var, foreground="#a00").pack(
            anchor="w", pady=(12, 0)
        )

        self.protocol("WM_DELETE_WINDOW", self._on_quit)

        self._refresh_ui()

        # Auto-start if configured and ready.
        if self.cfg.auto_start_bot and self.cfg.is_ready():
            self.after(200, self._start_bot)
        elif not self.cfg.is_ready():
            # First-run — open settings immediately.
            self.after(200, self._open_settings)

    # ---- actions ----

    def _toggle_bot(self) -> None:
        if self.bot.is_running():
            self._stop_bot()
        else:
            self._start_bot()

    def _start_bot(self) -> None:
        if not self.cfg.is_ready():
            self.info_var.set("Configure Telegram token and allowed user id first.")
            self._open_settings()
            return
        self.info_var.set("")
        self.status_var.set("● Bot: starting…")
        self.update_idletasks()
        try:
            self.bot.start(self.cfg.to_env())
        except Exception as e:
            log.exception("bot start failed")
            self.info_var.set(f"Start failed: {e}")
        err = self.bot.error()
        if err:
            self.info_var.set(f"Bot error: {err}")
        self._refresh_ui()

    def _stop_bot(self) -> None:
        self.status_var.set("● Bot: stopping…")
        self.update_idletasks()
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self) -> None:
        self.bot.stop()
        self.after(0, self._refresh_ui)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, self.cfg)
        self.wait_window(dlg)
        if dlg.result is not None:
            was_running = self.bot.is_running()
            if was_running:
                self.bot.stop()
            self.cfg = dlg.result
            self._refresh_ui()
            if was_running or (self.cfg.auto_start_bot and self.cfg.is_ready()):
                self._start_bot()

    def _open_downloads(self) -> None:
        d = self.cfg.effective_output_dir()
        d.mkdir(parents=True, exist_ok=True)
        open_folder(str(d))

    def _open_config_dir(self) -> None:
        open_folder(str(app_support_dir()))

    def _on_quit(self) -> None:
        if self.bot.is_running():
            self.status_var.set("● Bot: stopping…")
            self.update_idletasks()
            self.bot.stop()
        self.destroy()

    # ---- render ----

    def _refresh_ui(self) -> None:
        if self.bot.is_running():
            self.status_var.set("● Bot: running")
            self.status_lbl.configure(foreground="#0a7d20")
            self.toggle_btn.configure(text="Stop bot")
        else:
            self.status_var.set("● Bot: stopped")
            self.status_lbl.configure(foreground="#a00")
            self.toggle_btn.configure(text="Start bot")
        out = self.cfg.effective_output_dir()
        ready = "✓ ready" if self.cfg.is_ready() else "⚠ configure in Settings"
        self.path_var.set(f"{ready}  ·  saving to {out}")


# ---------- entry ----------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Starting musicdl-app on %s", platform.platform())
    log.info("Config: %s", config_path())
    win = MainWindow()
    win.mainloop()


if __name__ == "__main__":
    main()
