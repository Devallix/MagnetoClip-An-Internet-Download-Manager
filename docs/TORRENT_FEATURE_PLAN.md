# MagnetoClip — Torrent Download Feature Plan

**Feature:** Torrent Downloading (BitTorrent / Magnet Links)
**Library:** libtorrent v2.0.13
**Status:** Planned — Not Yet Implemented
**Date:** 2026-08-18

---

## 1. Decisions Summary

| Decision | Choice |
|----------|--------|
| Torrent library | `libtorrent` v2.0.13 (C++ bindings, pre-built wheels for Python 3.9-3.14) |
| Features | Magnet links, .torrent files, DHT, sequential download, file selection, seeding |
| Save location | Separate "Default Torrent Directory" setting alongside existing download folder |
| UI | New dedicated "Torrents" page in the sidebar |
| Search | Built-in torrent site scraper (yts.bz, etc.) + manual link/file input |
| Dependency | Core (always installed) |

---

## 2. Architecture Overview

### 2.1 libtorrent ↔ Qt Integration Pattern

libtorrent runs its own threads and uses an alert-based callback system. The
integration pattern bridges this to PySide6/qasync:

```
┌─────────────────────────────────────────────────────────────┐
│  PySide6 / qasync Event Loop                                 │
│                                                              │
│  ┌──────────────────┐    ┌────────────────────────────────┐ │
│  │ TorrentClient     │    │ Alert Poller                    │ │
│  │ (singleton)       │───▶│ - poll every 100ms              │ │
│  │                   │    │ - pop_alerts()                  │ │
│  │ - add_torrent()   │    │ - translate to Qt signals       │ │
│  │ - pause/resume()  │    │ - update DownloadManager state  │ │
│  │ - get_status()    │    └────────────────────────────────┘ │
│  └──────────────────┘                                         │
│           │                                                  │
│           ▼                                                  │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ libtorrent.session (own internal threads)                 ││
│  │ - Networking (Boost.Asio)                                ││
│  │ - Disk I/O                                               ││
│  │ - DHT, PEX, tracker communication                       ││
│  └──────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

Key rules:
- Alert processing runs on a QTimer (100ms interval) or asyncio.sleep() loop
- Status queries (`get_torrent_status()`, `get_peer_info()`) run in `asyncio.to_thread()` to avoid blocking the GUI thread
- Use `async_add_torrent()` instead of `add_torrent()` to avoid blocking

### 2.2 DownloadManager Integration

Add a third dispatch branch in `DownloadManager.start()`:

```python
if is_torrent_url(download.url):
    return self._start_torrent(download_id)
elif is_streaming_url(download.url):
    return self._start_streaming(download_id)
else:
    # existing HTTP path via _run()
```

---

## 3. New Module Structure

```
src/magnetoclip/torrent/
├── __init__.py
├── client.py      -- libtorrent session wrapper (singleton)
├── types.py       -- TorrentSpec, TorrentStatus dataclasses
├── handler.py     -- Per-torrent download orchestrator
├── resume.py      -- Fast-resume persistence (libtorrent resume data)
├── search.py      -- Torrent site search engine
└── sites.py       -- Site configuration registry
```

---

## 4. New Files to Create

| File | Purpose |
|------|---------|
| `src/magnetoclip/torrent/__init__.py` | Package init |
| `src/magnetoclip/torrent/client.py` | libtorrent session manager |
| `src/magnetoclip/torrent/types.py` | TorrentSpec, TorrentStatus dataclasses |
| `src/magnetoclip/torrent/handler.py` | Per-torrent download orchestrator |
| `src/magnetoclip/torrent/resume.py` | Fast-resume persistence |
| `src/magnetoclip/torrent/search.py` | Torrent site search engine |
| `src/magnetoclip/torrent/sites.py` | Site configuration registry |
| `src/magnetoclip/ui/pages/torrents.py` | Torrents page UI |
| `src/magnetoclip/ui/dialogs/add_torrent.py` | Add torrent dialog |
| `src/magnetoclip/ui/dialogs/torrent_details.py` | Torrent details dialog |
| `src/magnetoclip/ui/dialogs/torrent_search.py` | Search results dialog |

---

## 5. Files to Modify

| File | Changes |
|------|---------|
| `src/magnetoclip/core/downloads/manager.py` | Add torrent dispatch in `start()`, add `_start_torrent()`, `_run_torrent()` methods |
| `src/magnetoclip/core/downloads/model.py` | Add torrent status fields |
| `src/magnetoclip/database/models.py` | Add torrent columns to `Download` model, add `torrent_search_history` table |
| `src/magnetoclip/database/repositories.py` | Add torrent-specific queries |
| `src/magnetoclip/database/migrations.py` | Migration for new columns |
| `src/magnetoclip/config/settings.py` | Add torrent settings |
| `src/magnetoclip/app/context.py` | Create and wire `TorrentClient` singleton |
| `src/magnetoclip/app/lifecycle.py` | Initialize torrent client on startup, shutdown on exit |
| `src/magnetoclip/ui/main_window.py` | Add Torrents sidebar button + page |
| `src/magnetoclip/ui/categories.py` | Add "torrent" type |
| `src/magnetoclip/ui/dialogs/add_url.py` | Accept magnet URIs |
| `src/magnetoclip/core/categories/manager.py` | Add `.torrent` extension rule |
| `pyproject.toml` | Add `libtorrent>=2.0.13` to dependencies |
| `requirements.txt` | Add `libtorrent>=2.0.13` |

---

## 6. Database Schema Changes

### 6.1 New columns on `downloads` table

| Column | Type | Purpose |
|--------|------|---------|
| `torrent_info_hash` | `String(40)` | SHA-1 info hash for identification |
| `torrent_num_peers` | `Integer` | Current connected peer count |
| `torrent_num_seeds` | `Integer` | Current seed count |
| `torrent_num_pieces` | `Integer` | Total piece count |
| `torrent_piece_size` | `Integer` | Piece size in bytes |
| `torrent_sequential` | `Boolean` | Sequential download mode |
| `torrent_seeding` | `Boolean` | Currently seeding |

### 6.2 New table: `torrent_search_history`

| Column | Type | Purpose |
|--------|------|---------|
| `id` | `Integer` (PK) | Auto-increment |
| `query` | `String` | Search query text |
| `site` | `String` | Site searched |
| `results_json` | `Text` | Cached results |
| `timestamp` | `DateTime` | When the search was performed |

---

## 7. Settings Additions

```python
# Torrent settings (in config/settings.py)
torrent_default_save_dir: str = ""       # Falls back to main save_dir if empty
torrent_listen_port: int = 6881
torrent_enable_dht: bool = True
torrent_enable_pex: bool = True
torrent_enable_encryption: bool = True
torrent_max_upload_speed: int = 0        # 0 = unlimited
torrent_max_download_speed: int = 0      # 0 = unlimited
torrent_max_connections: int = 200
torrent_max_uploads: int = 4
torrent_default_sequential: bool = False
torrent_auto_seed: bool = False
torrent_search_sites: list[str] = ["yts"]  # Enabled search sites
```

---

## 8. UI Design

### 8.1 Torrents Page Layout

```
╭─────────────────────────────────────────────────────────────╮
│ Torrents                                    [Search] [Add]  │
├─────────────────────────────────────────────────────────────┤
│ Search torrents...                    [Site: v ▾] [🔍]     │
│                                                             │
│ ┌─ Search Results ────────────────────────────────────────┐ │
│ │ The.Matrix.1999.1080p.BluRay...  2.4GB  ▲1.2k  ▼56   │ │
│ │ Inception.2010.1080p.BRRip...    1.8GB  ▲890   ▼120   │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─ Active Torrents ──────────────────────────────────────┐ │
│ │ Ubuntu.24.04.iso                                      │ │
│ │ ███████████████░░░░  78%   12.4 MB/s   32 peers       │ │
│ │ Pieces: 1247/1600  Seeds: 8  Peers: 32  ETA: 2m 31s  │ │
│ │ [Pause] [Stop] [Open]                                 │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─ Seeding ──────────────────────────────────────────────┐  │
│ │ Blender.4.0.zip          ▲2.1 MB/s   Ratio: 1.4     │  │
│ └────────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│ ● Connected   12.4 MB/s   3 Active   1 Seeding            │
╰─────────────────────────────────────────────────────────────╯
```

### 8.2 Add Torrent Dialog

- Accept magnet URI, .torrent file path, or .torrent URL
- Show torrent metadata (name, files, total size) before confirming
- File selection checkboxes for multi-file torrents
- Sequential download toggle
- Save directory override
- Category and queue assignment

### 8.3 Torrent Details Dialog

- Torrent info: name, hash, total size, piece size, piece count
- Tracker list with status
- Peer list (IP, client, speed, flags)
- File list with per-file progress
- Transfer stats: uploaded, downloaded, ratio, seeds, peers

---

## 9. Torrent Search & Site Integration

### 9.1 Site Configuration (`torrent/sites.py`)

```python
SITES = {
    "yts": {
        "name": "YTS",
        "base_url": "https://yts.mx",
        "search_url": "https://yts.mx/api/v2/list_movies.json?query_term={query}",
        "type": "json_api",
        "parser": "yts_json",
    },
    # Extensible for more sites later (1337x, RARBG, etc.)
}
```

### 9.2 Search Engine (`torrent/search.py`)

- `search_torrents(query, site)` → list of `TorrentResult`
- Fields: name, size, seeds, leechers, magnet, torrent_url, poster, quality
- Uses `httpx.AsyncClient` for fetching
- Rate limiting per site
- Result caching

---

## 10. Implementation Phases

### Phase 1: Core Torrent Engine
- Create `torrent/client.py` — libtorrent session wrapper
- Create `torrent/types.py` — TorrentSpec, TorrentStatus dataclasses
- Create `torrent/handler.py` — per-torrent download orchestrator
- Create `torrent/resume.py` — fast-resume persistence
- Unit tests

### Phase 2: Database Schema Changes
- Add torrent columns to `Download` model
- Add `torrent_search_history` table
- Migration script

### Phase 3: Settings Updates
- Add torrent settings to `config/settings.py`
- Settings UI additions

### Phase 4: DownloadManager Integration
- Add torrent dispatch in `start()`
- Add `_start_torrent()`, `_run_torrent()` methods
- Event bus integration

### Phase 5: UI
- Torrents page (`ui/pages/torrents.py`)
- AddTorrentDialog (`ui/dialogs/add_torrent.py`)
- TorrentDetailsDialog (`ui/dialogs/torrent_details.py`)
- Sidebar navigation update

### Phase 6: Torrent Search
- Search engine (`torrent/search.py`)
- Site parsers (`torrent/sites.py`)
- Search UI in Torrents page

### Phase 7: Integration Testing
- End-to-end testing
- Resume/crash recovery
- Edge cases

---

## 11. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| libtorrent C++ bindings crash/segfault | Use try/except wrappers, run libtorrent calls in threads, log errors |
| Alert polling performance | Batch-process alerts, throttle UI updates to ~10/sec |
| libtorrent blocking getters freeze UI | All status queries via asyncio.to_thread() |
| Site scraper breakage (HTML changes) | Modular site parsers, graceful fallback, user can disable sites |
| Large torrent file lists (10k+ files) | Virtual/lazy model for file lists, paginate search results |
| Seeding indefinitely | Configurable auto-stop rules (ratio limit, time limit, idle limit) |
| Python 3.14 compatibility | libtorrent v2.0.13 has confirmed 3.14 wheels on PyPI |

---

## 12. Reference Projects

- **Ghost-Downloader-3** (github.com/XiaoYouChR/Ghost-Downloader-3) — Production PySide6 + libtorrent download manager with 5k+ stars. Proves the integration pattern works.
- **qBittorrent** — Uses libtorrent as its core engine, demonstrates advanced features.
- **libtorrent docs** — https://libtorrent.org/reference.html

---

## 13. Dependencies Added

```
libtorrent>=2.0.13        # Core BitTorrent engine (C++ bindings, pre-built wheels)
```

Optional companion (for .torrent file parsing):
```
torrentool>=1.2.0         # .torrent file parser (pure Python, for metadata extraction)
```
