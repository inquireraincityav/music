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

## DJ-set tracklist discovery

When you use `!set <url>`, `musicdl` tries to find a tracklist in this order:

1. **Video description**, looking for timestamped `01:23 Artist - Title` lines.
2. **Top-level YouTube comments** (fallback if description has no timestamps).
3. **1001tracklists.com** — first checks the description for a
   `1001tracklists.com/...` URL (many DJs link theirs), then does a
   DuckDuckGo web search for `"<video title>" 1001tracklists`.
4. Fetches the discovered page — first via plain `requests` with a browser
   User-Agent, and if Cloudflare's challenge page comes back, retries via
   Playwright (headless Chromium). Playwright is *optional*: skip its install
   and the bot degrades to requests-only, catching the Cloudflare-blocked
   cases via the paste-manually fallback.

If everything fails, the bot replies with the URL it found (if any) and asks
you to open it in a browser and paste the tracklist back with `!tracklist`.

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
| `/restart` | Exit; your process manager restarts the bot. |
| `/whoami` | Reply with your Telegram user id. |

Files still land in `~/Desktop/MusicDownloads` on the machine running the
bot — the phone just triggers the work.

**Security note:** `/shell` gives Telegram-message-level shell access to the
allowlisted users. Keep `MUSICDL_SHELL_ENABLED=1` off unless you understand
that, and keep the allowlist tight. Anyone with your bot token can pretend
to be the bot, so treat it like a password.

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
