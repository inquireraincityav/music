# musicdl

Local CLI that fetches songs, playlists, or DJ-set tracklists as **320 kbps MP3**
files organised under `~/Desktop/MusicDownloads`.

Give it a URL (YouTube, SoundCloud, Hypeddit, DJcity, BPMSupreme download
pages, Spotify, etc.) *or* a free-text search like `"Fisher - Losing It (VIP
Mix)"` and it will pick the right version, download it, transcode to
320 kbps MP3, embed the thumbnail as cover art, and drop the file into the
right folder.

## Requirements

- Python **3.9+**
- **ffmpeg** on `PATH` (used for transcoding to MP3 320 kbps)
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - Windows: `winget install ffmpeg` or grab a build from
    <https://www.gyan.dev/ffmpeg/builds/>

## Install

```bash
git clone https://github.com/inquireraincityav/music.git
cd music
python3 -m venv .venv
source .venv/bin/activate         # on Windows: .venv\Scripts\activate
pip install -e .
```

That installs the `musicdl` command into the venv.

## Output layout

```
~/Desktop/MusicDownloads/
├── Singles/          # standalone tracks
├── Playlists/
│   └── <Playlist Name>/     # numbered per playlist order
└── Sets/
    └── <Set Name>/          # numbered per tracklist order
```

Override the root with `--output /some/other/dir` or the env var
`MUSICDL_OUTPUT_DIR`.

## Usage

### Single track from a URL

```bash
musicdl "https://soundcloud.com/artist/song-vip-edit"
musicdl "https://youtu.be/dQw4w9WgXcQ"
musicdl "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
```

### Search when you don't have a link

```bash
musicdl "Fisher - Losing It"
musicdl --artist "Fisher" --title "Losing It" --variant "VIP Mix"
```

The `--variant` flag steers the search toward the right remix/edit/extended
mix rather than the original.

### Playlists (SoundCloud sets, YouTube playlists, Spotify playlists)

```bash
musicdl "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx"
musicdl "https://open.spotify.com/playlist/37i9dQZF1DX0XUsuxWHRQd"
```

Tracks are named `01 - Title.mp3`, `02 - …`, in the playlist's own order,
inside `Playlists/<Playlist Name>/`.

If a link is a single video that also has an "index" query param and you
*want* the whole playlist, force it with `--playlist`.

### YouTube DJ sets (parse tracklist from description/comments)

```bash
musicdl --set "https://www.youtube.com/watch?v=abcdefghijk"
```

`musicdl` reads the video's description (and comments as a fallback), pulls
out the timestamped tracklist, and downloads each track separately by
searching for the correct version — so a 2-hour Solomun set becomes ~25
individually-named files inside `Sets/<Set Name>/`.

If the tracklist isn't discoverable automatically, paste it into a text file
and use:

```bash
musicdl --tracklist-file mySet.txt --name "Solomun @ Diynamic 2024"
```

Each line can be `HH:MM Artist - Title` or just `Artist - Title`.

## Desktop app (musicdl-app) — GUI on Mac + Windows

Instead of running the bot from a terminal, install a double-clickable app.
The Telegram bot runs inside the app while it's open, so downloads happen
on whichever machine has the app running when a URL comes in from your
phone. Close the app → bot stops cleanly.

### Try it in dev (no packaging yet)

Inside your existing venv:

```bash
pip install -e .
musicdl-app
```

On first launch a settings dialog opens — paste your Telegram bot token,
your numeric user id, pick a download folder, save. Bot starts polling.

Config lives at:

- macOS  : `~/Library/Application Support/musicdl/config.json`
- Windows: `%APPDATA%\musicdl\config.json`
- Linux  : `~/.config/musicdl/config.json`

The CLI (`musicdl`) and the terminal bot (`musicdl-bot`) still work; the
GUI is just a friendlier front door.

### Build a real .app / .exe (bundled ffmpeg, no Python required on target)

Run the build script **on** the target platform (no cross-compile):

**macOS** (Apple Silicon or Intel — the script picks the right ffmpeg):

```bash
./build/build_mac.sh
# Output: dist/musicdl.app
open dist/musicdl.app
```

Move it to `/Applications`. On first launch macOS may block the unsigned
app — right-click → **Open** → **Open Anyway**.

**Windows** (from a `cmd` prompt or PowerShell in the repo root):

```bat
build\build_windows.bat
REM Output: dist\musicdl.exe
```

Double-click `dist\musicdl.exe`. First launch: SmartScreen may warn about
an unrecognized app — click **More info** → **Run anyway**.

The bundled app carries its own static ffmpeg, so the target machine
doesn't need Python, doesn't need ffmpeg installed, doesn't need
Homebrew — just download the app and open it.

Signed builds (no security warnings) require Apple/Microsoft developer
certificates and aren't done here yet.

## DJ-set tracklist discovery

When you use `!set <url>`, `musicdl` tries to find a tracklist in this order:

1. **YouTube chapters** (uploader-structured chapter marks — highest-signal
   source when present).
2. **Video description**, looking for timestamped `01:23 Artist - Title`
   lines, numbered lists, or plain `Artist - Title` lines under a
   `Tracklist:` header.
3. **Top-level YouTube comments**, pinned ones first (DJs often pin the
   tracklist as a comment).
4. **1001tracklists.com** — first checks the description for a
   `1001tracklists.com/...` URL (many DJs link theirs), then does a
   DuckDuckGo web search for `"<video title>" 1001tracklists`.
5. Fetches the discovered page — first via plain `requests` with a browser
   User-Agent, and if Cloudflare's challenge page comes back, retries via
   Playwright (headless Chromium). Playwright is *optional*: skip its install
   and the bot degrades to requests-only, catching the Cloudflare-blocked
   cases via the paste-manually fallback.

If everything fails, the bot replies with the URL it found (if any) and asks
you to open it in a browser and paste the tracklist back with `!tracklist`.

### Per-track search fallbacks

For each tracklist entry, if the exact `Artist - Title (Mix) feat. X` query
returns nothing, musicdl retries with progressively looser variants before
giving up on that track:

1. Original query — unchanged.
2. Strip `feat. X` / `ft. X` from the title.
3. Strip `(…)` / `[…]` version blocks entirely.
4. Flatten artist + title into one search string.
5. Last resort: the raw tracklist line.

Version qualifier matching stays enforced at every step — we never silently
fall back to the "original mix" when you asked for a VIP. The failure report
tells you exactly which variants were tried.

### Enabling the Playwright fallback

```bash
pip install '.[browser]'          # or: pip install playwright
playwright install chromium       # ~200MB download, one-time
```

After that, restart `musicdl-bot`. The web-discovery path will start using
the headless browser when Cloudflare blocks a plain fetch.

## Telegram bot (chat from your phone)

`musicdl-bot` lets you drive everything above from a Telegram chat.

### 1. Create the bot

- On Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` →
  follow prompts → copy the API token.
- Message [@userinfobot](https://t.me/userinfobot) to get your own numeric
  Telegram user id (or start the bot and send `/whoami` — unauthorised users
  are told their own id).

### 2. Configure

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
export TELEGRAM_ALLOWED_USER_IDS="111222333"   # comma-separated
export MUSICDL_SHELL_ENABLED="1"               # optional: enables /shell
```

Anyone whose id isn't in `TELEGRAM_ALLOWED_USER_IDS` gets a polite refusal.

### 3. Run

```bash
musicdl-bot
```

To keep it running across reboots, wrap it in `systemd` (Linux),
`launchd` (macOS), or `nssm` (Windows). Example systemd unit:

```ini
# ~/.config/systemd/user/musicdl-bot.service
[Unit]
Description=musicdl Telegram bot
After=network-online.target

[Service]
Environment=TELEGRAM_BOT_TOKEN=...
Environment=TELEGRAM_ALLOWED_USER_IDS=...
Environment=MUSICDL_SHELL_ENABLED=1
WorkingDirectory=%h/music
ExecStart=%h/music/.venv/bin/musicdl-bot
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now musicdl-bot`.

### Chat protocol

| Message | Effect |
| --- | --- |
| `https://…` | Download that URL. If it's a playlist, expands and downloads each. If it's a long video (>20 min), treated as a DJ set: auto-parses tracklist and downloads each track, falling back to the whole video as one MP3 if no tracklist is found. Otherwise a single track. |
| `!set https://…` | Force set mode. Refuses to fall back to a full-video download; asks you to paste manually if no tracklist is found. |
| `!full https://…` | Skip tracklist parsing and grab the whole video as one MP3. |
| `!playlist https://…` | Force playlist mode. |
| `!search Artist - Title` | Free-text search. |
| `!tracklist <name>` (multiline body) | Download each line as a track. Use when `!set` can't find a tracklist in the video. First line is the set name, following lines are `Artist - Title` (timestamped lines also OK). |
| `… --variant "Extended Mix"` | Steer to the right remix/edit version. |
| `/git pull` | Pull latest code from the repo. |
| `/shell <cmd>` | Run a shell command *(only if `MUSICDL_SHELL_ENABLED=1`)*. |
| `/restart` | Restart the bot in place (GUI: stops+starts the bot thread; terminal: re-execs the process). |
| `/whoami` | Reply with your Telegram user id. |
| `/queue` | List any jobs that were in flight when the bot last restarted. |
| `/queue clear` | Drop everything from the pending queue. |
| `/status` | Show what's downloading right now (bar, byte counts, current track in a set). |
| `/cancel` | Abort the active download / set at the next byte and stop looping tracks. |
| `/health` | Diagnostic: python / musicdl path, yt-dlp + ffmpeg versions, output dir writability + free disk, cookies file, pending queue depth, active job, bot uptime. |

Files still land in `~/Desktop/MusicDownloads` on the machine running the
bot — the phone just triggers the work.

**Security note:** `/shell` gives Telegram-message-level shell access to the
allowlisted users. Keep `MUSICDL_SHELL_ENABLED=1` off unless you understand
that, and keep the allowlist tight. Anyone with your bot token can pretend
to be the bot, so treat it like a password.

## Authenticated services (BPMSupreme, DJcity, private SoundCloud, etc.)

For services where downloads require you to be logged in — paid DJ pools
like BPMSupreme and DJcity, private SoundCloud sets, age-restricted
YouTube — export your browser cookies to a Netscape-format `cookies.txt`
and point `musicdl` at it:

```bash
export MUSICDL_COOKIES_FILE="/path/to/cookies.txt"
```

Both the CLI (`musicdl`) and the bot (`musicdl-bot`) will pick it up. The
same file is forwarded to `spotdl` too, so any authenticated fallbacks
also work.

Recommended cookie export tool (browser extension, one click):

- Chrome/Edge: [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
- Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)

Log in to the target site in your browser first, then export cookies for
that site only (don't dump everything).

Anyone with your cookies file can act as you on those sites — treat it
like a password.

## Resume, duration verification, mirror folders

**Resume.** Re-sending the same URL (or re-running a half-finished set)
skips tracks already on disk. The check is duration-based: the file has to
exist in the target folder *and* its measured duration has to match what the
source URL reports (within 5 s / 3 %). A corrupt or truncated file won't
masquerade as a complete one.

**Duration verification.** Every fresh download is probed with `ffprobe`
after transcoding; if the measured mp3 is more than 5 s / 3 % off the
expected duration, the file is deleted and the download fails loudly. Catches
silent ffmpeg truncations that previously left 30-second fragments on disk.

**Mirror folders.** Point `MUSICDL_MIRROR_DIRS` at one or more extra output
roots (colon-separated on Mac/Linux, semicolon on Windows) and every
finished mp3 is copied into each one, preserving the
`Singles/Playlists/Sets` sub-path:

```bash
export MUSICDL_MIRROR_DIRS="$HOME/Library/Mobile Documents/com~apple~CloudDocs/MusicDownloads"
```

With that set, both Macs' output folders stay in sync via iCloud without
running a second bot on the travel machine. Mirror copies happen after the
primary write succeeds; a copy failure logs a warning but doesn't fail the
download. Configure this from the desktop app's **Settings → Mirror folders**
field (one path per line) rather than the env var.

**Persistent queue.** Every job is written to
`<app-support>/state/queue.json` before processing and removed on
completion. If the bot (or the whole machine) crashes mid-download, send
`/queue` on restart to see what didn't finish; `/queue clear` to drop it.
Fully automatic resume is a follow-up — this version gives you visibility
and manual re-send.

**Desktop notifications.** When any download finishes, musicdl shows a
native OS toast (macOS Notification Center, Windows toast, Linux
`notify-send`). Disable via the app's Settings or `MUSICDL_NOTIFY_ENABLED=0`.

**Launch at login.** Tick the box in Settings and the app registers itself
with your OS to start when you log in (LaunchAgent on Mac, HKCU Run key on
Windows, `~/.config/autostart` on Linux). No `launchd` plist editing.

## Notes

- The 320 kbps figure is the **MP3 encode bitrate**. Real audio fidelity is
  capped by whatever the source stream provides — a lossy stream doesn't
  become lossless just because ffmpeg wrote 320 kbps.
- Some sites' Terms of Service restrict downloading. Use `musicdl` against
  sources where you have the right to download (paid DJ pools such as
  BPMSupreme / DJcity, artist giveaways on Hypeddit, Creative-Commons
  material, your own uploads, etc.).
- Spotify streams are DRM-protected, so `spotdl` (used for Spotify URLs)
  resolves each track to a YouTube equivalent and downloads that.

## Troubleshooting

- **`ffmpeg not found`**: install ffmpeg and re-open the shell.
- **`ERROR: Sign in to confirm your age`** on some YouTube videos: see
  [yt-dlp cookies docs](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp).
- **Wrong version downloaded** for a search: add `--variant "Extended Mix"`
  or paste the exact URL of the track instead.
