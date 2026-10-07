"""Cross-platform desktop notifications.

Uses the OS's native toast mechanism:
  macOS   : `osascript -e 'display notification ...'` (always available)
  Windows : PowerShell BurntToast / fallback to `msg.exe`
  Linux   : `notify-send`

Failure is silent — notifications are nice-to-have, never block downloads.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("musicdl.notify")

_ENABLED_ENV = "MUSICDL_NOTIFY_ENABLED"


def notifications_enabled() -> bool:
    val = os.environ.get(_ENABLED_ENV, "1")
    return val not in ("", "0", "false", "False")


def _escape_apple(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, body: str = "") -> None:
    if not notifications_enabled():
        return
    try:
        if sys.platform == "darwin":
            _mac(title, body)
        elif sys.platform == "win32":
            _windows(title, body)
        else:
            _linux(title, body)
    except Exception as e:  # noqa: BLE001 — notifications must never raise
        log.debug("notify failed: %s", e)


def _mac(title: str, body: str) -> None:
    script = f'display notification "{_escape_apple(body)}" with title "{_escape_apple(title)}"'
    subprocess.run(["osascript", "-e", script], check=False, timeout=5)


def _windows(title: str, body: str) -> None:
    # BurntToast is the clean way but may not be installed; try native toast
    # via PowerShell with a WinRT API first, else fall back to msg.exe.
    ps_script = (
        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;'
        '$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('
        '[Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
        f'$t.GetElementsByTagName("text")[0].AppendChild($t.CreateTextNode("{title}")) > $null;'
        f'$t.GetElementsByTagName("text")[1].AppendChild($t.CreateTextNode("{body}")) > $null;'
        '$n = [Windows.UI.Notifications.ToastNotification]::new($t);'
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("musicdl").Show($n);'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=False,
            timeout=8,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _linux(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, body], check=False, timeout=5)
