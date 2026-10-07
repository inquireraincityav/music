# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for musicdl desktop app.

Builds a single-file app on Mac (.app bundle) and Windows (.exe). ffmpeg
is expected to be present at ./build/vendor/ffmpeg (unix) or
./build/vendor/ffmpeg.exe (Windows) — the platform build script downloads
the right static build before running pyinstaller.

Run via:
  Mac      :  ./build/build_mac.sh
  Windows  :  build\\build_windows.bat

Not via `pyinstaller musicdl.spec` directly — the ffmpeg staging happens
in the shell script.
"""
import sys
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path.cwd()
VENDOR_DIR = PROJECT_ROOT / "build" / "vendor"
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

ffmpeg_name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
ffmpeg_src = VENDOR_DIR / ffmpeg_name

binaries = []
if ffmpeg_src.is_file():
    # Place ffmpeg next to the app's Python entry so sys._MEIPASS finds it.
    binaries.append((str(ffmpeg_src), "."))

a = Analysis(
    ["musicdl/app.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        # Tkinter's ttk themes and some yt-dlp extractors need explicit hints.
        "musicdl.bot",
        "musicdl.downloader",
        "musicdl.tracklist",
        "musicdl.tracklist_web",
        "musicdl.spotify",
        "musicdl.organizer",
        "musicdl.config",
        "musicdl.app_config",
        "musicdl.app_bot_manager",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="musicdl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=IS_MAC,  # macOS Finder file-open events
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if IS_MAC:
    app = BUNDLE(
        exe,
        name="musicdl.app",
        icon=None,
        bundle_identifier="com.raincityav.musicdl",
        info_plist={
            "CFBundleName": "musicdl",
            "CFBundleDisplayName": "musicdl",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
