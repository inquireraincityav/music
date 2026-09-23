@echo off
REM Build musicdl.exe for Windows.
REM Downloads a static ffmpeg build, stages it, runs PyInstaller.
REM Output: dist\musicdl.exe

setlocal enabledelayedexpansion
cd /d "%~dp0.."

if not exist .venv (
    echo -^> creating venv
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo -^> installing build deps
pip install -q -e . pyinstaller

set VENDOR=build\vendor
if not exist %VENDOR% mkdir %VENDOR%

if not exist %VENDOR%\ffmpeg.exe (
    echo -^> downloading ffmpeg
    set FF_ZIP=%TEMP%\ffmpeg-release.zip
    set FF_URL=https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip
    powershell -NoProfile -Command "Invoke-WebRequest -Uri '%FF_URL%' -OutFile '%FF_ZIP%'"
    powershell -NoProfile -Command "Expand-Archive -Path '%FF_ZIP%' -DestinationPath '%TEMP%\ffmpeg-extract' -Force"
    for /r "%TEMP%\ffmpeg-extract" %%f in (ffmpeg.exe) do copy /y "%%f" "%VENDOR%\ffmpeg.exe"
    rmdir /s /q "%TEMP%\ffmpeg-extract"
    del "%FF_ZIP%"
)

echo -^> ffmpeg staged: %VENDOR%\ffmpeg.exe
echo -^> running pyinstaller
if exist build\musicdl rmdir /s /q build\musicdl
if exist dist\musicdl.exe del /q dist\musicdl.exe
pyinstaller --clean --noconfirm build\musicdl.spec

echo.
echo Built: dist\musicdl.exe
echo Double-click to run. First launch: Windows SmartScreen may warn — click
echo "More info" then "Run anyway" (the app is unsigned).
endlocal
