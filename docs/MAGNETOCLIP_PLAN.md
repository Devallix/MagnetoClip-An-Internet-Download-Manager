# MagnetoClip — Final Product Development Plan

**Product:** MagnetoClip
**Descriptor:** Advanced Download Manager
**Tagline:** Capture the Web.
**Framework:** Python + PySide6 (Qt6)
**Database:** SQLite (SQLAlchemy 2.0)
**Architecture:** Modular / asynchronous
**Primary platform:** Windows (architecture prepared for macOS/Linux)
**Design:** Premium dark UI with radiant violet/blue/cyan accents
**Primary font:** Inter · **Brand font:** Space Grotesk
**Core metaphor:** Magnetic attraction

---

## 1. Product Vision

MagnetoClip is a high-performance desktop download manager designed to compete with established products such as IDM while delivering a significantly more modern, intelligent, and extensible experience.

The goal is **not** to make a "Python version of IDM." The goal is to take everything that works about IDM, remove its dated UX limitations, and build a modern download platform around it.

Core philosophy:

> **Capture → Analyze → Accelerate → Download → Verify → Organize**

Combined capabilities:

- Multi-connection downloading
- Intelligent connection management
- Automatic resume
- Download acceleration
- Browser integration
- Media/resource detection
- Scheduling
- Queue management
- File organization
- Download history
- Bandwidth management
- Proxy support
- Authentication
- Integrity verification
- Advanced diagnostics
- Modern analytics
- Powerful customization

## 2. Product Positioning

```
             MODERN UX
                 ▲
                 │
                 │       MAGNETOCLIP
                 │          ★
                 │
                 │
     FDM ────────┼──────────── IDM
                 │
                 │
                 ▼
            BASIC USERS
```

IDM-level functionality + modern software architecture + premium UI + intelligent automation.

**Competitive pillars:**

- ⚡ **Performance** — aggressive but intelligent connection optimization
- 🧠 **Intelligence** — automatic decisions instead of forcing configuration on users
- ✨ **Design** — a genuinely modern desktop experience
- 🛡 **Reliability** — downloads survive failures
- 🌐 **Integration** — browser + desktop + future remote control
- 📊 **Visibility** — users understand exactly what their downloads are doing
- 🧩 **Extensibility** — modular architecture for future capabilities

---

## 3. Environment Audit (verified 2026-08)

| Component | Status |
|---|---|
| Python | 3.14.6 installed |
| PySide6 | 6.11.1 installed |
| httpx | 0.28.1 installed |
| SQLAlchemy | 2.0.51 installed |
| pytest / pytest-asyncio / pytest-qt | installed |
| aiohttp, aioftp, keyring, structlog, aiofiles, qasync, PyInstaller | not installed |
| ffmpeg | not on PATH |
| Project directory | empty (greenfield) |

**Confirmed technical decision:** the engine is **httpx-only** (sync + async). `aiohttp`/`aioftp` are dropped from the core stack because Python 3.14 wheels for them are unreliable. httpx 0.28.1 already works on Python 3.14. FTP is deferred; if required later, use `aioftp` behind a plug-in interface or thread-based `ftplib`.

**Dependencies to add** (Phase 1): `qasync`, `keyring`, `structlog`, `aiofiles`.

---

## 4. Core Technical Decisions

1. **Async engine on the Qt main thread via `qasync`** — one asyncio event loop drives all downloads; UI updates via Qt signals. Fallback if qasync breaks on Python 3.14: dedicated `QThread` running the asyncio loop + thread-safe signal bridge.
2. **HTTP: httpx only** — sync `httpx.Client` for CLI/repository work, async `httpx.AsyncClient` for the download engine.
3. **Segmentation via HTTP Range** — each segment is an independent async httpx request writing to a `.part` file with its own retry/backoff state.
4. **SQLite via SQLAlchemy 2.0** for all persistent data; a **`.mclip` JSON sidecar** per active download for crash-safe resume state.
5. **Credentials via `keyring`** (platform secure store). TLS verification always on. All file paths sanitized (path-traversal protection).
6. **Logging via `structlog`** with JSON output. "Export Diagnostic Report" scrubs credentials.
7. **Threading rule:** network/hashing I/O is async; CPU-heavy work (hashing chunks, metadata, encryption) → `asyncio.to_thread`/ThreadPoolExecutor. The Qt UI thread never blocks.
8. **FFmpeg not bundled in v1** — the media subsystem detects ffmpeg at runtime, documents the dependency, and exposes a configurable path setting.

---

## 5. Final Project Structure

```
magnetoclip/
├── pyproject.toml / requirements.txt
├── README.md
├── docs/MAGNETOCLIP_PLAN.md
├── src/magnetoclip/
│   ├── app/main.py                    # entrypoint, qasync bootstrap
│   ├── app/lifecycle.py               # app init/shutdown, single-instance lock
│   ├── app/context.py                 # DI container: settings, db, engine refs
│   ├── config/settings.py             # typed settings model + load/save
│   ├── core/
│   │   ├── downloads/manager.py       # DownloadManager (facade over engine)
│   │   ├── downloads/model.py         # Download dataclass + state enum
│   │   ├── queues/manager.py          # queues + queue_items
│   │   ├── scheduler/scheduler.py     # time/day/bandwidth schedules
│   │   ├── categories/manager.py      # categories + auto-categorization rules
│   │   └── events/bus.py              # pub/sub event bus (Qt signals)
│   ├── engine/
│   │   ├── downloader/engine.py       # MagnetoCore orchestration
│   │   ├── downloader/segment.py      # single segment downloader
│   │   ├── downloader/allocator.py    # adaptive connection allocator
│   │   ├── segmenter/planner.py       # range splitting
│   │   ├── retry/policy.py            # error classification + backoff
│   │   ├── resume/mclip.py            # .mclip serialize/deserialize
│   │   └── verification/hasher.py     # md5/sha1/sha256/sha512/blake2
│   ├── network/
│   │   ├── http/client.py             # httpx factory, TLS, headers
│   │   ├── http/range.py              # Range request helpers
│   │   ├── proxy/profiles.py          # proxy profile model + resolution
│   │   ├── auth/credentials.py        # keyring wrapper (Basic/Bearer/cookies)
│   │   └── headers/builder.py         # UA, referer, cookies, custom headers
│   ├── browser/
│   │   ├── native_messaging/host.py   # native messaging host (stdin/stdout JSON)
│   │   └── integration/rules.py       # capture rules
│   ├── media/
│   │   ├── detection/detector.py      # stream/manifest sniffing
│   │   ├── metadata/reader.py         # optional metadata extraction
│   │   └── ffmpeg/bridge.py           # optional ffmpeg wrapper
│   ├── database/
│   │   ├── models.py                  # SQLAlchemy ORM
│   │   ├── repositories.py            # data-access layer
│   │   └── migrations.py              # simple VERSION table migrator
│   ├── security/
│   │   ├── credentials.py             # secure credential storage
│   │   ├── validation.py              # URL/input validation
│   │   └── safe_names.py              # filename/path sanitization
│   ├── services/
│   │   ├── notification/notifier.py   # system tray notifications
│   │   ├── filesystem/paths.py        # default dirs, free space checks
│   │   ├── analytics/collector.py     # download statistics collection
│   │   ├── analytics/dashboard.py     # chart data aggregation
│   │   └── diagnostics/report.py      # diagnostic report export
│   ├── ui/
│   │   ├── main_window.py
│   │   ├── pages/overview.py
│   │   ├── pages/downloads.py
│   │   ├── pages/queue.py
│   │   ├── pages/completed.py
│   │   ├── pages/scheduler.py
│   │   ├── pages/analytics.py
│   │   ├── pages/categories.py
│   │   ├── pages/browser.py
│   │   ├── pages/settings.py
│   │   ├── widgets/download_card.py
│   │   ├── widgets/progress.py
│   │   ├── widgets/detail_panel.py
│   │   ├── widgets/speed_chart.py
│   │   ├── dialogs/add_download.py
│   │   ├── dialogs/new_queue.py
│   │   ├── dialogs/settings_dialog.py
│   │   ├── components/sidebar.py
│   │   ├── components/statusbar.py
│   │   ├── components/tray.py
│   │   ├── components/notifications.py
│   │   ├── themes/dark.qss
│   │   ├── themes/light.qss
│   │   ├── themes/palette.py
│   │   └── themes/animations.py
│   └── resources/                      # icons, Inter + Space Grotesk fonts
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── engine/
│   ├── network/
│   ├── ui/
│   ├── stress/
│   └── conftest.py                     # local test HTTP server fixture
├── scripts/                            # dev tools, build.py
├── browser-extension/                  # Manifest V3 extension + native host
└── requirements.txt
```

---

## 6. Database Schema (SQLAlchemy 2.0)

- **downloads** — id, url, filename, save_path, category_id, queue_id, size_total, size_downloaded, status, speed_avg, speed_peak, priority, connections_max, connections_active, proxy_profile_id, headers_json, auth_ref, etag, last_modified, hash_algo, hash_expected, hash_calculated, created_at, started_at, completed_at, error, retry_count
- **download_segments** — id, download_id, index, start_byte, end_byte, downloaded, status, attempts
- **categories** — id, name, folder, icon, color, rules_json (extension/type rules for auto-categorization)
- **queues** — id, name, max_concurrent, schedule_id
- **queue_items** — id, queue_id, download_id, position
- **schedules** — id, name, start_time, end_time, days_mask, speed_day, speed_night, enabled
- **settings** — key (PK), value_json
- **browser_events** — id, source, url, detected_type, ts
- **download_statistics** — id, download_id, ts, speed, connections, bandwidth_used
- **proxy_profiles** — id, name, type (http/https/socks5), host, port, username_ref
- **verified_runs** — id, download_id, algo, result, ts

---

## 7. Download Engine — MagnetoCore

### 7.1 State Machine

```
queued → connecting → downloading → paused
            │              │            │
            │              ├────────────┴──▶ verifying → completed
            │              └──▶ retrying → downloading
            │                       │
            └──▶ failed │ stopped │ scheduled
```

States: `queued`, `scheduled`, `connecting`, `downloading`, `paused`, `verifying`, `completed`, `failed`, `stopped`, `retrying`.

### 7.2 Download Flow

1. **Analyze** — HEAD/GET to discover: total size, `Accept-Ranges`, `Content-Length`, ETag, Last-Modified, content type.
2. **Plan** — split into segments (start: 2 connections; segment sizes derived from total size and connection count).
3. **Execute** — segments download in parallel via `httpx.AsyncClient` with Range headers, writing to `.part` files.
4. **Verify** — after merge, run optional hash check (prefer SHA-256 / BLAKE2).
5. **Complete** — move to final path, record history, notify.

### 7.3 Adaptive Connection Allocator (signature feature)

Rather than blindly creating 16 connections:

```
Initial → 2 connections → measure → 4 connections → measure → 8 → optimize
```

Rules:
- Double connections (2 → 4 → 8 → 16) while per-connection throughput improves ≥10% per doubling.
- Cap at `connections_max`.
- Only allocate when server honors `Accept-Ranges` (206 responses).
- On 416 (range not satisfiable) or misbehaving servers, drop to single connection.
- Stop creating connections when they stop improving throughput (avoids wasting resources and overwhelming servers).

### 7.4 Resume System

Downloads survive app crashes, restarts, network failures, server failures, and sleep/wake cycles.

`.mclip` sidecar (JSON), e.g. `Ubuntu.iso.mclip`:

```json
{
  "url": "https://...",
  "file_path": "C:/Downloads/Ubuntu.iso",
  "total_size": 6442450944,
  "etag": "\"abc123\"",
  "last_modified": "Tue, 05 Aug 2026 12:00:00 GMT",
  "headers": {"User-Agent": "..."},
  "hash_algo": "sha256",
  "hash_expected": "XXXX",
  "state": "downloading",
  "segments": [
    {"index": 0, "start": 0, "end": 805306368, "written": 805306368},
    {"index": 1, "start": 805306368, "end": 1610612736, "written": 600000000}
  ]
}
```

On startup: scan for orphaned `.mclip` + `.part` pairs and offer resume. Persist state on every event batch (throttled to ~1/s).

### 7.5 Smart Retry System

Error classification → recovery strategy:

```
Connection failure → classify → temporary? → yes: retry with backoff / no: reconfigure
```

- **Exponential backoff with jitter** — 1s, 2s, 4s, 8s, … capped at 60s.
- Server-aware retries: `429` (respect `Retry-After`), `5xx` (backoff), TLS errors (reconnect), network-change (immediate resume).
- Segment-scoped retries: only the failed segment reconnects.
- Connection replacement: a stalled connection is replaced with a fresh one.
- Retry-limit exhaustion → `failed` with a structured error.

### 7.6 Verification Pipeline

Support MD5, SHA-1, SHA-256, SHA-512, BLAKE2 (prefer SHA-256 / BLAKE2).

```
Download → file verification → integrity check → completed
                             └── failed → user retries or redownloads
```

Hashing runs chunk-by-chunk (1 MB) via `asyncio.to_thread` so it never blocks the UI.

---

## 8. UI Design (PySide6)

### 8.1 Main Window Layout

```
╭────────────────────────────────────────────────────╮
│ MagnetoClip                         ─ □ ×          │
├───────────────┬────────────────────────────────────┤
│               │                                    │
│  ◉ Overview   │     DOWNLOADS                     │
│               │                                    │
│  ↓ Downloads  │  Search downloads...               │
│               │                                    │
│  ◷ Queue      │  ┌──────────────────────────────┐ │
│               │  │ Ubuntu.iso                   │ │
│  ✓ Completed  │  │ ███████████████░░  78%      │ │
│               │  │ 8.4 MB/s • 2m 31s remaining │ │
│  ⏰ Scheduler │  └──────────────────────────────┘ │
│               │                                    │
│  📊 Analytics │  ┌──────────────────────────────┐ │
│               │  │ Blender.zip                  │ │
│  ⚙ Settings   │  │ ████████████████████ 100%  │ │
│               │  │ Download complete            │ │
│               │  └──────────────────────────────┘ │
│               │                                    │
├───────────────┴────────────────────────────────────┤
│ ● Connected             8.42 MB/s        3 Active │
╰────────────────────────────────────────────────────╯
```

### 8.2 Sidebar Navigation

Overview · Downloads · Queue · Completed · Scheduler · Analytics · Categories · Browser · Settings. Potential future: **Magneto Center** (advanced system/network information).

### 8.3 Download Detail Panel

Status, progress %, speed, connections (12/16), downloaded/total, time remaining, per-segment connection bars, network interface, server host, integrity status.

### 8.4 Context Menu

Start · Pause · Resume · Stop · Restart · Open File · Open Folder · Copy URL · Copy Download Information · Verify Integrity · Change Priority · Move to Queue · Schedule · Remove · Remove and Delete File.

### 8.5 System Tray

Shows active count + aggregate speed. Actions: Open MagnetoClip · Pause All · Resume All · Bandwidth · Settings · Exit.

### 8.6 Notifications

Native `QSystemTrayIcon.showMessage` for: Download Complete / Download Paused (network lost) / Download Resumed (connection restored). Subtle and non-intrusive.

### 8.7 Theme System

- **Dark** — primary MagnetoClip experience (radiant violet/blue/cyan accents).
- **Light** — clean professional alternative.
- Future: **System** (follow OS preference).

QSS files (`themes/*.qss`) + palette object. Frameless custom titlebar; Inter for UI, Space Grotesk for branding.

### 8.8 Analytics Dashboard

Today's stats: downloaded GB, downloads count, average speed, peak speed. Charts: daily downloads, weekly bandwidth, average speed, peak speed, file categories, network usage. Custom QPainter charts in v1 (no heavy chart dependency).

---

## 9. Queues, Scheduling, Bandwidth

### 9.1 Queues

Users create named queues (e.g. "Software"): Ubuntu.iso → VSCode.exe → Python.exe → Git.exe. Options: start/pause/stop/reorder, schedule, limit simultaneous downloads, auto-advance to next item.

### 9.2 Scheduler

Time windows (e.g. 01:00–06:00), day masks (Mon–Sun), bandwidth per window (daytime max 2 MB/s, night unlimited).

### 9.3 Bandwidth Management

Global: Unlimited / 10 MB/s / 5 MB/s / 2 MB/s / 1 MB/s / Custom. Per-download limits. Implement with a token-bucket rate limiter in the async engine.

### 9.4 Network Awareness

States: ● Connected · Slow · Limited · Offline. Network changes can trigger actions (pause on Wi-Fi drop, resume on Ethernet detect, etc.). Use a periodic probe + system events.

### 9.5 Categories

Automatic categorization: Documents, Videos, Music, Images, Software, Archives, Torrents, Other. Extension/type rules; user-customizable folders and rules.

---

## 10. Browser Integration

Architecture: Manifest V3 browser extension + **native messaging host** (`browser/native_messaging/host.py`, stdin/stdout JSON protocol).

Flow:

```
Browser click → MagnetoClip Extension → Resource Analysis → MagnetoClip → Download Dialog
```

User choices in capture dialog: **Download Now · Download Later · Add to Queue · Download With Category**.

Targets: Chrome, Edge, Firefox, Chromium-based browsers.

### 10.1 Resource Detection

Direct files, documents, archives, images, audio, video, installers, ISO files, large datasets. Future: embedded media, streaming manifests.

---

## 11. Networking Layer

- **Proxy support:** HTTP, HTTPS, SOCKS5, authentication, per-download proxy profiles (Direct / Office / Personal / Custom).
- **Authentication:** Basic, Bearer tokens, cookies, custom headers, Referer, User-Agent, session auth. Credentials never stored in plaintext (keyring).
- **HTTP/2:** supported where the stack allows; **HTTP/3/QUIC** designed as an isolated transport plug-in so it can be added without rewriting the engine.

---

## 12. Media Engine (Phase 7)

Separate subsystem, not mixed into the core engine. Stream/manifest detection, metadata reading, and an optional **ffmpeg bridge** for audio extraction, video processing, format conversion, and merging supported streams (where supported and legally appropriate). ffmpeg detected at runtime; no hard dependency in v1.

---

## 13. File Safety & Security

- Normalize and sanitize all paths before writing — never trust filenames like `../../something.exe`.
- Prevent path traversal, invalid filenames, unexpected filesystem locations.
- TLS verification always on.
- Input validation for URLs and download parameters.
- Secure temporary files (`SecureTemp` pattern).
- Dependency auditing before release.

---

## 14. Performance Architecture

Qt UI thread is never blocked.

```
                 PySide6 UI
                     │
              Application Layer
                     │
              Async Controller (qasync event loop)
                     │
             Download Scheduler
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
   Download 1   Download 2   Download 3
        │            │            │
      Async        Async        Async
        │            │            │
        └────────────┼────────────┘
                     ↓
               Network Layer (httpx)
```

Heavy CPU work (hashing, metadata) → `asyncio.to_thread` / ThreadPoolExecutor.

---

## 15. Logging & Diagnostics

- `structlog` with JSON output.
- Log: network failures, retry events, segment failures, browser events, database errors, application errors.
- **Export Diagnostic Report** scrubs all credentials.

---

## 16. Build Order (Development Phases)

1. **Foundation** — repo layout, pyproject, settings, logging, DB + models + migrations, event bus, qasync bootstrap, empty themed shell window.
2. **MagnetoCore** — single-connection httpx download with progress/cancel → Range segmentation → pause/resume → `.mclip` persistence → retry/backoff → hashing. Engine testable headless.
3. **Download Manager** — multi-download concurrency, queues, categories, priority, scheduler, bandwidth throttling, history.
4. **Modern UI** — full design system: sidebar, dashboard, download cards, detail panel, settings pages, dark/light themes, animations, notifications, tray.
5. **Browser integration** — extension + native messaging host + capture dialog.
6. **Advanced networking** — proxies, auth, headers/cookies, connection tuning, network detection.
7. **Media engine** — detection, metadata, optional ffmpeg.
8. **Intelligence** — adaptive allocator, auto-categorization, smart scheduling, network-aware bandwidth, server capability detection.
9. **Analytics** — statistics + dashboard charts.
10. **Security & hardening** — audit, path-traversal testing, credential protection, crash/recovery testing.
11. **Testing** — full layered suite (below).
12. **Packaging** — PyInstaller + Inno Setup → `MagnetoClip-Setup.exe` + `MagnetoClip-Portable.zip`. Verify latest PyInstaller supports Python 3.14; fallback: Nuitka.

---

## 17. MVP Definition (v0.1 → v0.5)

**Core:** HTTP/HTTPS, multi-connection, resume, pause, retry, cancel, queue, multiple simultaneous downloads, download history, categories, scheduling, bandwidth control.
**UI:** MagnetoClip branding, modern dark theme, dashboard, download list, download details, settings, notifications, system tray.
**Reliability:** crash recovery, persistent download state, integrity checking, structured logging.

---

## 18. Version Roadmap

- **0.1** — Core Engine (basic download functionality)
- **0.5** — Advanced Download Manager (queues, scheduler, categories, bandwidth)
- **0.9** — Browser Integration (capture from browsers)
- **1.0** — *Capture the Web.* Stable public release
- **1.5** — Intelligent Download Engine (adaptive connections, optimization)
- **2.0** — Magneto Platform (remote control, plugins, CLI, advanced media, integrations)

Future (architecture-influencing only): **MagnetoClip Remote** API, CLI (`magnetoclip add/pause/resume/list/remove/queue`), plugin system (`plugins/browser, media, protocols, exporters, integrations`).

---

## 19. Testing Strategy

Layers: Unit → Integration → Download Engine → Network → UI (pytest-qt) → Stress → Failure Recovery → End-to-End.

- `pytest` + `pytest-asyncio` + `pytest-qt`.
- Local test HTTP server fixture (range/partial/large/slow/flaky/failing endpoints).
- Mock network layer for deterministic engine tests.
- Test scenarios: 1 GB downloads, interrupted downloads, server disconnects, network changes, computer sleep, application crash (kill process), invalid URLs, expired links, partial downloads, multiple simultaneous downloads.

---

## 20. Packaging (Phase 12)

- Windows: PyInstaller + Inno Setup → `MagnetoClip-Setup.exe`, `MagnetoClip-Portable.zip`.
- Future: `MagnetoClip.dmg`, `MagnetoClip.AppImage`, `MagnetoClip.deb`.
- Signed releases; dependency audit.

---

## 21. Top Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Python 3.14 wheel availability (aiohttp/aioftp) | httpx-only engine; optional libs isolated behind plug-in interfaces |
| qasync compatibility | Fallback: dedicated-loop QThread + signal bridge |
| PyInstaller Python 3.14 support | Verify at packaging phase; fallback Nuitka |
| ffmpeg absence | Runtime detection, configurable path, no hard dep in v1 |
| Server refuses Range requests | Auto-fallback to single connection |
| Credential leakage in logs/reports | Scrubbing in diagnostics; keyring storage |

---

## 22. Recommended First Implementation Session (Phase 1)

1. Create `pyproject.toml` + `requirements.txt` (add `qasync`, `keyring`, `structlog`, `aiofiles`; pin `httpx`, `PySide6`, `SQLAlchemy`).
2. Scaffold `src/magnetoclip/…` package layout + `app/main.py` qasync bootstrap showing an empty themed window.
3. Config (`config/settings.py`) + `structlog` setup + SQLite init with the full schema (Section 6).
4. Event bus (`core/events/bus.py`) + unit tests.
5. Run test suite + verify app launches.
