"""Launch-at-login support for musicdl-app.

Mac: writes a LaunchAgent plist to ~/Library/LaunchAgents/com.musicdl.app.plist
     Load/unload with `launchctl`.
Windows: writes a value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.
Linux: writes a .desktop autostart entry to ~/.config/autostart/.

All three are per-user, no admin needed. Toggling off removes the entry.
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("musicdl.autostart")

LABEL = "com.musicdl.app"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _desktop_entry_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "musicdl-app.desktop"


def _app_launch_cmd() -> list[str]:
    """Command the OS should run to launch the app at login.

    If we're frozen (PyInstaller .app / .exe), use the bundle launcher.
    Otherwise prefer the console script `musicdl-app` on PATH.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundle. sys.executable is the launcher itself.
        return [sys.executable]
    cli = shutil.which("musicdl-app")
    if cli:
        return [cli]
    # Fallback: python -m musicdl.app from the current interpreter.
    return [sys.executable, "-m", "musicdl.app"]


# ---------- Mac ----------


def _mac_set(enabled: bool) -> tuple[bool, str]:
    path = _plist_path()
    if not enabled:
        if path.exists():
            try:
                subprocess.run(["launchctl", "unload", str(path)], check=False)
            except OSError:
                pass
            path.unlink(missing_ok=True)
            return True, "disabled"
        return True, "already disabled"
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = _app_launch_cmd()
    plist = {
        "Label": LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / "musicdl-app.out.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / "musicdl-app.err.log"),
    }
    with path.open("wb") as f:
        plistlib.dump(plist, f)
    try:
        subprocess.run(["launchctl", "unload", str(path)], check=False)
        subprocess.run(["launchctl", "load", str(path)], check=False)
    except OSError as e:
        return False, f"wrote plist but launchctl failed: {e}"
    return True, "enabled"


# ---------- Windows ----------


def _windows_set(enabled: bool) -> tuple[bool, str]:
    try:
        import winreg  # type: ignore
    except ImportError:
        return False, "winreg unavailable"
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_READ,
    )
    try:
        if not enabled:
            try:
                winreg.DeleteValue(key, "musicdl-app")
                return True, "disabled"
            except FileNotFoundError:
                return True, "already disabled"
        cmd = _app_launch_cmd()
        value = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        winreg.SetValueEx(key, "musicdl-app", 0, winreg.REG_SZ, value)
        return True, "enabled"
    finally:
        winreg.CloseKey(key)


# ---------- Linux ----------


def _linux_set(enabled: bool) -> tuple[bool, str]:
    path = _desktop_entry_path()
    if not enabled:
        if path.exists():
            path.unlink()
            return True, "disabled"
        return True, "already disabled"
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = _app_launch_cmd()
    exec_line = " ".join(cmd)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=musicdl\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return True, "enabled"


# ---------- public API ----------


def set_launch_at_login(enabled: bool) -> tuple[bool, str]:
    """Enable or disable launch-at-login. Returns (ok, message)."""
    try:
        if sys.platform == "darwin":
            return _mac_set(enabled)
        if sys.platform == "win32":
            return _windows_set(enabled)
        return _linux_set(enabled)
    except Exception as e:  # noqa: BLE001
        log.exception("autostart toggle failed")
        return False, str(e)


def launch_at_login_enabled() -> bool:
    if sys.platform == "darwin":
        return _plist_path().exists()
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore
        except ImportError:
            return False
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            )
            try:
                winreg.QueryValueEx(key, "musicdl-app")
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except OSError:
            return False
    return _desktop_entry_path().exists()
