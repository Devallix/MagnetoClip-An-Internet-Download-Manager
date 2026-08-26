# MagnetoClip

**Advanced Download Manager — Capture the Web.**

MagnetoClip is a Windows desktop download manager built with Python + PySide6.
It pairs an IDM-class multi-connection download core with a browser extension
that detects files, media streams and torrents as you browse — then captures
them into smart, auto-sorted categories.

Current version: **0.2.7**

## Documentation

- [User guide](docs/USER_GUIDE.md) — end-user manual: every page, dialog and setting
- [Product development plan](docs/MAGNETOCLIP_PLAN.md)
- [Torrent feature plan](docs/TORRENT_FEATURE_PLAN.md)
- [EULA](docs/EULA.md)
- [License server README](license-server/README.md) — running and deploying the licensing backend

## Features

### Download engine

- **Multi-segment HTTP downloads** — byte-range splitting with adaptive
  connection allocation (up to 64 connections per download, 32 simultaneous)
- **Resume support** — interrupted downloads continue from exact byte offsets
  via `.mclip` JSON sidecar files, surviving restarts and crashes
- **Automatic retries** — exponential backoff on transient failures (0–20 retries)
- **Integrity verification** — finished files pass hashing checks
  (MD5 / SHA1 / SHA256 / SHA512 / BLAKE2b) before being marked complete
- **Global bandwidth cap** — optional MB/s throttle across all transfers
- Per-download controls: connection count, proxy profile, basic-auth
  credentials, custom cookies, custom filename, save folder, category

### Browser integration

Companion MV3 extension for **Chrome, Edge, Firefox, Brave, Vivaldi and
Chromium**, connected over native messaging:

- **Page scanning** detects downloadable files as you browse; results collect
  on the *Detected* page with type, size and source page
- **Capture confirmation popup** per file: Download now / Download later /
  Skip / Skip all
- **Media stream capture** for video and audio
- **Default-downloader mode** intercepts every browser download
- **`blob:` URL resolution** — fetches browser-only blob resources through
  the extension in chunked transfer
- One-click native-messaging host install per browser; auto-install via
  browser policy once the extension is store-published

### Streaming media

- Dedicated engine (yt-dlp based) supporting 30+ popular video/music sites,
  plus direct HLS (`.m3u8`) and DASH (`.mpd`) streams
- Quality picker: Best / 1080p / 720p / Audio only
- Automatic track merging and metadata handling via FFmpeg tooling

### Torrents

- Full BitTorrent client powered by **libtorrent**: magnet URIs and `.torrent`
  files with metadata preview (name, size, tracker, info hash, file list)
- DHT, PEX and protocol encryption (all toggleable), configurable listen port
  and peer/upload limits
- Torrent queue with active-torrent and active-download slots
- **Sequential downloading** for near-instant preview playback
- Seeding controls: auto-seed after completion or manual "Start Seeding"
- Windows integration: `.torrent` file association and `magnet:` protocol
  handler registration
- Peers/seeds column, per-torrent progress, speed and ETA

### Smart organization & analytics

- Eight categories (Image, Video, Audio, Document, Compressed, App/Software,
  Torrent, Other) with 70+ auto-categorization extension rules
- Overview dashboard: live active/completed counts, total downloaded, current
  speed, recent activity cards
- Analytics page: totals, average and peak speed, 14-day download/bandwidth
  charts, category breakdown — computed locally, nothing leaves your PC

### Remote dashboard

- Control MagnetoClip from your phone or another device **on the local
  network** (aiohttp server on port 8477)
- Token-authenticated (Bearer + HMAC) with QR-code pairing
- Regenerating the token revokes all paired devices instantly
- Never exposes the filesystem

### Licensing

- Serial-key activation (`MGCL-XXXXX-…`), one PC per key with machine binding
- Ed25519-signed license responses with pinned public key — clients reject
  spoofed endpoints
- Serial stored in the OS keyring (Windows Credential Manager); self-service
  deactivation frees the slot for another PC
- Friendly error states for revoked / expired / machine-limit / network cases
- Bundled Flask + SQLite license server with a web admin panel
  (generate, revoke, re-enable, unbind activations) — see
  [license-server/README.md](license-server/README.md)

### Desktop experience

- Dark and light themes, splash screen, animated sidebar and page transitions
- System tray with pause-all/resume-all, remote pairing, license info and
  balloon notifications that deep-link to files or detected items
- Context menus everywhere: open/reveal/restart/retry/copy URL/remove
- In-app updates: manifest check → verified download (SHA-256) → automatic
  apply-and-restart
- Proxy profile manager (HTTP / HTTPS / SOCKS5) with keyring-stored secrets

### Security

- Filename sanitization: strips paths, invalid characters and reserved names
  (180-char cap); path-traversal defense on every write
- Credentials never stored in plaintext — OS keyring only, with audits
- URL scheme validation before any request

## Requirements

- Windows 10/11
- Python ≥ 3.11 (for development)

Key dependencies: PySide6, httpx, SQLAlchemy 2.0, qasync, libtorrent,
yt-dlp, aiohttp, cryptography, keyring, structlog, segno.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run the application
$env:PYTHONPATH = "src"
python -m magnetoclip

# Run tests
pytest
```

### License server (local)

```powershell
cd license-server
pip install -r requirements.txt
$env:MCLIP_ADMIN_PASSWORD = "dev-password"
python run_local.py 8490        # admin panel: http://127.0.0.1:8490/admin
```

Development builds ship with an empty `license.endpoint`, which disables the
activation gate entirely; set `MCLIP_LICENSE_OFF=1` to force it off as well.

### Packaging

Release zips live under `releases/`; the PyInstaller spec output is built into
`build/magnetoclip/`. Release builds must embed the production license-server
URL and Ed25519 public key in `src/magnetoclip/config/settings.py`
(`license.endpoint` / `license.public_key`) before packaging.

## Layout

```
src/magnetoclip/
├── app/          # entrypoint, lifecycle, DI context
├── config/       # settings model
├── core/         # downloads, queues, scheduler, categories, events, proxies
├── database/     # SQLAlchemy models, repositories, migrations (SQLite, WAL)
├── engine/       # download engine (segments, resume, retry, verification)
├── intelligence/ # speed prediction, bandwidth fair-share allocation
├── network/      # HTTP, proxy, auth, cookies, headers
├── browser/      # browser integration / native messaging host
├── media/        # streaming detection, yt-dlp + ffmpeg bridge, metadata
├── torrent/      # libtorrent wrapper, magnets, associations, search
├── security/     # credentials, validation, path safety, audit
├── services/     # logging, notifications, analytics, diagnostics,
│                 #   licensing, remote dashboard, updates
└── ui/           # PySide6 pages, dialogs, widgets, themes
license-server/   # Flask + SQLite licensing backend + web admin panel
docs/             # plans, user guide, EULA
tests/            # unit / service / UI / browser / media test suites
```

## License

Proprietary — see [docs/EULA.md](docs/EULA.md). Developed by Devallix.
