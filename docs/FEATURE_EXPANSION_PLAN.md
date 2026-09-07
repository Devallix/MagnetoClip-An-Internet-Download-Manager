# MagnetoClip Feature Expansion Plan

Six new features that will set MagnetoClip apart from every other download manager.

---

## Implementation Order

| Phase | Feature | Why First |
|---|---|---|
| 1 | Download Rules Engine | Foundation — other features benefit from rules |
| 2 | Duplicate Detection | Quick win, high user value, simple pipeline integration |
| 3 | Built-in File Preview | Improves UX of existing downloads, no backend pipeline changes |
| 4 | Pause & Auto-Resume | Replaces the planned weekly windows with one unambiguous global switch + countdown timer |
| 5 | Network Speed Monitor | Self-contained service, rich visual page |
| 6 | Webpage Archiver | Most complex, depends on UI infrastructure from other features |

---

## Feature 1: Download Rules Engine

**Goal:** User-defined automation rules: "When a download matches X, do Y." The killer differentiator.

### Database — new `download_rules` table

```python
class DownloadRule(Base):
    __tablename__ = "download_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = default(True)
    priority: Mapped[int] = default(0)  # higher = runs first
    # Condition fields (all optional — NULL means "match any")
    match_url_pattern: Mapped[str | None] = mapped_column(String(512))  # regex
    match_filename_pattern: Mapped[str | None] = mapped_column(String(512))  # regex
    match_min_size: Mapped[int | None] = mapped_column(BigInteger)  # bytes
    match_max_size: Mapped[int | None] = mapped_column(BigInteger)
    match_category: Mapped[str | None] = mapped_column(String(64))
    match_extension: Mapped[str | None] = mapped_column(String(32))  # e.g. ".pdf"
    match_host: Mapped[str | None] = mapped_column(String(256))  # e.g. "github.com"
    # Action fields (all optional)
    action_set_category: Mapped[str | None] = mapped_column(String(64))
    action_set_save_dir: Mapped[str | None] = mapped_column(String(512))
    action_set_connections: Mapped[int | None] = mapped_column(Integer)
    action_set_priority: Mapped[int | None] = mapped_column(Integer)
    action_auto_start: Mapped[bool] = default(False)
    action_rename_pattern: Mapped[str | None] = mapped_column(String(256))
    action_max_bandwidth: Mapped[int | None] = mapped_column(Integer)  # MB/s cap
    action_tag: Mapped[str | None] = mapped_column(String(128))
```

### Backend (`core/rules/`)

- **`manager.py`** — `RuleManager`: CRUD for rules, in-memory sorted cache, `reload()`, fires `RULES_CHANGED`.
- **`engine.py`** — `RuleEngine`: single method `evaluate(download) -> RuleResult`:
  1. Iterates rules by priority descending.
  2. For each enabled rule, tests all non-NULL conditions against the download's URL, filename, size, category, extension, host.
  3. Returns first matching rule's actions (or None).
  4. Uses `re.search` for pattern matches, extracted host via `urllib.parse.urlparse`.
- **`applier.py`** — `RuleApplier`: applies `RuleResult` actions to a download.

### Integration

`DownloadManager.add()` calls `RuleEngine.evaluate()` after auto-categorization, applies actions before persisting.

### UI

- **New page** (`ui/pages/rules.py`): table-based. Columns: Enabled, Name, Conditions, Actions, Priority. Toolbar: Add, Edit, Delete, Toggle, Reorder.
- **New dialog** (`ui/dialogs/rule_editor.py`): two-panel — conditions (URL/filename regex with tester, size range, category, extension, host) + actions (category, save dir, connections, priority, auto-start, rename pattern with `{hostname}/{filename}/{ext}/{category}`). "Test Rule" button.

### Settings

- `rules.enabled` (bool, default True)
- `rules.on_add_only` (bool, default True)

### Nav

Add `("rules", "Rules")` to `NAV_ITEMS` after Analytics.

### Effort: Large (~700 lines)

---

## Feature 2: Duplicate Detection

**Goal:** Before downloading, check if the user already has the file.

### Database — new `file_hashes` table

```python
class FileHash(Base):
    __tablename__ = "file_hashes"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(String(4096))
    filename: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger)
    hash_algo: Mapped[str] = mapped_column(String(32))
    hash_value: Mapped[str] = mapped_column(String(128))
    category_id: Mapped[int | None] = mapped_column(FK("categories.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

### Backend (`core/dedup/`)

- **`manager.py`** — `DedupManager`: `index_download()`, `check_duplicate(url, filename, size)`, `scan_directory()`, `prune()`, `stats()`.
- **`hasher.py`** — xxhash via `asyncio.to_thread`.
- **`types.py`** — `DuplicateResult` dataclass.

### Integration

- `DownloadManager.add()`: call `check_duplicate()` before creating record. Show dialog unless `auto_skip` is True.
- `_finalize()`: call `index_download()` after completion.

### UI

- `DuplicateResultDialog`: "Skip" / "Download Anyway" / "Open Existing".
- Overview page: "Files Indexed" StatCard.
- Settings: "Duplicate Detection" section.

### Settings

- `dedup.enabled`, `dedup.auto_skip`, `dedup.hash_algo`, `dedup.index_after_download`

### Effort: Medium (~450 lines)

---

## Feature 3: Built-in File Preview

**Goal:** Preview images, play video/audio, view PDFs inside MagnetoClip.

### Backend (`services/preview/`)

- **`resolver.py`** — `PreviewResolver`: determines previewability. Returns `PreviewType` enum (IMAGE, VIDEO, AUDIO, PDF, TEXT, NONE).

| Type | Extensions | Implementation |
|---|---|---|
| Image | jpg, png, gif, webp, bmp, svg, ico | `QLabel` + `QPixmap`, wheel zoom, pan |
| Video | mp4, mkv, webm, avi | `QVideoWidget` + `QMediaPlayer` |
| Audio | mp3, flac, ogg, wav, aac, m4a | Audio player with seek bar |
| PDF | pdf | `QPdfDocument` or system fallback |
| Text | txt, json, csv, xml, log, md (≤5MB) | Read-only `QPlainTextEdit` |

### UI

- **`PreviewDialog(QDialog)`**: modal viewer, resizable, toolbar (zoom, fit, play/pause, close).
- **`PreviewToolButton`**: icon button for `DownloadCard` and context menus.

### Integration

- `DownloadCard`: preview button. Context menus: "Preview" for completed downloads. `download_details.py`: "Preview" button.

### Settings

- `preview.enabled`, `preview.video_player`, `preview.pdf_viewer`

### Effort: Medium (~400 lines)

---

## Feature 4: Pause & Auto-Resume

**Goal:** A simple, unambiguous pause control. The original weekly-window
*Scheduled Download Windows* design was prototyped and then replaced by this
lighter feature: pause everything with one switch and optionally resume
automatically after N hours.

### State — persisted in settings (no new table)

The pause state is stored in the existing settings table:

- `pause.enabled` — on/off for the global switch
- `pause.auto_resume_hours` — 0 disables auto-resume; 1–168 enables a
  one-shot countdown
- `pause.resume_at` — ISO timestamp of the scheduled auto-resume

### Backend (`core/pause/`)

- **`manager.py`** — `PauseController`: `pause()` / `resume()` /
  `set_auto_resume_hours()`, a 15 s asyncio tick that checks `resume_at` and
  flips back automatically, persists through `SettingsStore`, and posts
  `PAUSE_STATE_CHANGED`.

### Integration

- `DownloadManager.pause_all()` / `resume_all()` pause or start every running
  and waiting download; downloads added while paused stay in the paused list.
- `DownloadManager` subscribes to `PAUSE_STATE_CHANGED` to apply the switch.

### UI (`ui/pages/pause.py`)

- **Pause Downloads card**: "Pause all downloads" checkbox with a live status
  label.
- **Auto-Resume card**: hours spinbox (0–168, "0 h (off)") plus a live
  "Resuming in H h MM min" countdown while paused with a timer set.
- **How it works** card summarising the behaviour.

### Nav

Add `("pause", "Pause")` to `NAV_ITEMS` after Analytics.

### Effort: Small (~250 lines)

---

## Feature 5: Network Speed Monitor

**Goal:** Built-in speed test with historical tracking and ISP throttling detection.

### Database — new `speed_tests` table

```python
class SpeedTest(Base):
    __tablename__ = "speed_tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    download_mbps: Mapped[float] = mapped_column(Float)
    upload_mbps: Mapped[float] = mapped_column(Float)
    latency_ms: Mapped[float] = mapped_column(Float)
    server_name: Mapped[str | None] = mapped_column(String(128))
    server_url: Mapped[str | None] = mapped_column(String(512))
    isp_name: Mapped[str | None] = mapped_column(String(128))
    test_size_mb: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
```

### Backend (`services/speedtest/`)

- **`tester.py`** — `SpeedTester`: httpx-based download/upload test, latency probe. Fires `SPEED_TEST_PROGRESS` events.
- **`analyzer.py`** — `SpeedAnalyzer`: historical stats, throttle detection, ISP report, CSV export.
- **`scheduler.py`** — Optional periodic testing.

### UI (`ui/pages/speedtest.py`)

- Animated speed gauge (`QPainter`). Results as 3 `StatCard`s.
- History chart, throttle detection card, ISP info card, CSV export.

### Settings

- `speedtest.enabled`, `speedtest.test_server`, `speedtest.test_size_mb`, `speedtest.auto_test_enabled`, `speedtest.auto_test_interval_hours`, `speedtest.throttle_sensitivity`

### Nav

Add `("speedtest", "Speed")` to `NAV_ITEMS` after Pause.

> **Status:** implemented — see `ui/pages/speedtest.py` and
> `services/speedtest/` (full test = download + upload + latency, with
> per-phase progress and an animated gauge; upload-only runs still measure
> latency).

### Effort: Medium (~500 lines)

---

## Feature 6: Webpage Archiver

**Goal:** Save full webpages as self-contained HTML with embedded assets.

### Backend (`services/archiver/`)

- **`scraper.py`** — `PageScraper`: httpx fetch, HTML parse (selectolax/BeautifulSoup), extract resources, base64 inline, rewrite HTML.
- **`archiver.py`** — `PageArchiver`: `archive()`, `archive_with_metadata()`. Handles relative URLs, robots.txt.
- **`types.py`** — `ArchiveResult`.

### Integration

- Detect `detected_type == "webpage"` in `DownloadManager.add()`, route to `_run_archiving`.
- Browser extension: "Archive this page" button.
- Save as `<title> (<date>).html` in "Webpages" category.

### UI

- Browser Extension: archive button.
- `AddUrlDialog`: "Webpage Archive" mode.
- Preview: archived HTML via `QWebEngineView` or `QTextBrowser`.

### Settings

- `archiver.enabled`, `archiver.inline_images`, `archiver.inline_css`, `archiver.inline_js`, `archiver.max_resource_size`, `archiver.timeout`, `archiver.add_metadata`

### Effort: Large (~600 lines)

---

## Cross-Cutting Changes

### New Dependencies

- `xxhash` — fast hashing for duplicate detection
- `selectolax` or `beautifulsoup4` — HTML parsing for webpage archiver

### Database Migrations

New tables: `download_rules`, `file_hashes`, `speed_tests`.
New `DownloadStatus` value: `scheduled`.

### New Events

```python
RULES_CHANGED = "rules.changed"
RULE_MATCHED = "rules.matched"
DUPLICATE_DETECTED = "duplicate.detected"
PAUSE_STATE_CHANGED = "pause.state_changed"
SPEED_TEST_PROGRESS = "speedtest.progress"
SPEED_TEST_COMPLETED = "speedtest.completed"
ARCHIVE_COMPLETED = "archive.completed"
```

### New Pages

| Page | Nav Key | Position |
|---|---|---|
| Rules | `rules` | After Analytics |
| Pause | `pause` | After Analytics (implemented) |
| Speed Test | `speedtest` | After Pause (implemented) |

### New Dialogs

| Dialog | Used By |
|---|---|
| `RuleEditorDialog` | Rules page |
| `DuplicateResultDialog` | DownloadManager.add() |
| `PreviewDialog` | Download cards, context menus |

### New Manager/Service Classes

| Class | Location | Pattern |
|---|---|---|
| `RuleManager` | `core/rules/manager.py` | CRUD + cache (like CategoryManager) |
| `RuleEngine` | `core/rules/engine.py` | Stateless evaluator |
| `DedupManager` | `core/dedup/manager.py` | Index + lookup + cache |
| `PauseController` | `core/pause/manager.py` | Settings-backed state + tick loop (implemented) |
| `SpeedTestService` | `services/speedtest/__init__.py` | Facade (like AnalyticsService) |
| `WebpageArchiver` | `services/archiver/__init__.py` | Facade |
| `PreviewService` | `services/preview/__init__.py` | Facade |
