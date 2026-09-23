"""Config persistence for the musicdl desktop app.

Config lives in a platform-appropriate app-support directory (not in the
project or user shell env), so the GUI never needs the user to touch
environment variables:

  macOS   : ~/Library/Application Support/musicdl/config.json
  Windows : %APPDATA%\\musicdl\\config.json
  Linux   : ~/.config/musicdl/config.json
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


APP_NAME = "musicdl"


def app_support_dir() -> Path:
    """Return the platform-appropriate directory to store app config."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return app_support_dir() / "config.json"


def default_output_dir() -> Path:
    return Path.home() / "Desktop" / "MusicDownloads"


@dataclass
class AppConfig:
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: list[int] = field(default_factory=list)
    output_dir: str = ""  # empty means "use default_output_dir()"
    cookies_file: str = ""  # empty means none
    auto_start_bot: bool = True

    def is_ready(self) -> bool:
        """True if config is complete enough to start the bot."""
        return bool(self.telegram_bot_token) and bool(self.telegram_allowed_user_ids)

    def effective_output_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_output_dir()

    def to_env(self) -> dict[str, str]:
        """Env vars the bot process expects, derived from this config."""
        env = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_ALLOWED_USER_IDS": ",".join(
                str(uid) for uid in self.telegram_allowed_user_ids
            ),
            "MUSICDL_OUTPUT_DIR": str(self.effective_output_dir()),
        }
        if self.cookies_file:
            env["MUSICDL_COOKIES_FILE"] = self.cookies_file
        return env


def load_config() -> AppConfig:
    path = config_path()
    if not path.is_file():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    # Normalize allowlist to list[int] (older configs may have stored strings).
    ids = raw.get("telegram_allowed_user_ids") or []
    if isinstance(ids, str):
        ids = [p.strip() for p in ids.split(",") if p.strip()]
    normalized_ids: list[int] = []
    for uid in ids:
        try:
            normalized_ids.append(int(uid))
        except (TypeError, ValueError):
            continue
    return AppConfig(
        telegram_bot_token=str(raw.get("telegram_bot_token") or ""),
        telegram_allowed_user_ids=normalized_ids,
        output_dir=str(raw.get("output_dir") or ""),
        cookies_file=str(raw.get("cookies_file") or ""),
        auto_start_bot=bool(raw.get("auto_start_bot", True)),
    )


def save_config(cfg: AppConfig) -> None:
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    tmp.replace(path)
