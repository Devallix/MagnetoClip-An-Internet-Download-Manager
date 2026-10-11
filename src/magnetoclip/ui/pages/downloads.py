"""Downloads page: a table of downloads with per-row progress and controls."""

from __future__ import annotations

import base64
import datetime as _dt
import time
from pathlib import Path, PurePath, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.core.events.bus import Events

from ..categories import snapshot_in_category
from ..components.buttons import VerticalIconButton
from ..components.icons import tool_icon, type_icon
from ..components.liquid_progress import LiquidProgressBar
from ..dialogs.add_url import AddUrlDialog
from ..dialogs.download_details import DownloadDetailsDialog
from ..util import (
    format_bytes,
    format_eta,
    format_speed,
    fraction,
    open_path,
    reveal_path,
)
from .base import Page

TERMINAL = {"completed", "failed", "verification_failed", "stopped"}
ACTIVE = {"connecting", "downloading", "retrying", "verifying"}

_HEADERS = ("", "Name", "Size", "Status", "Speed", "Time left", "Time added")

# Above this many rows the table is filled in chunks so the page stays
# responsive; at or below it a single synchronous pass is fast enough and keeps
# refresh()'s behaviour predictable for callers that read the table straight
# after calling it.
_PROGRESSIVE_THRESHOLD = 150

# Progress events can fire several times per second per download, but the
# manager persists them to the DB at most once a second. Reading the DB again
# inside that window returns the same row, so per-download UI refreshes are
# throttled to roughly the DB cadence instead of tracking every event.
_UI_PROGRESS_THROTTLE = 0.75


class StatusCell(QWidget):
    """Status column: an animated liquid-colour progress bar plus a percent label."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 10, 4)
        layout.setSpacing(8)

        self.bar = LiquidProgressBar()
        self.bar.setObjectName("status_bar")
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.percent = QLabel("0%")
        self.percent.setObjectName("status_percent")

        layout.addWidget(self.bar, 1)
        layout.addWidget(self.percent)

    @staticmethod
    def _apply_state(widget, prop: str, value: str, polish: bool) -> None:
        """Set a dynamic property and re-evaluate the style only if it changed.

        ``polish()`` re-resolves the whole stylesheet for the widget, which costs
        milliseconds. Doing it per row per refresh made loading a large history
        take seconds, so it now runs only when the state genuinely changed, and
        never for rows that have not been styled yet (a widget that has never
        been polished computes the correct style on first paint anyway).
        """
        if widget.property(prop) == value:
            return
        widget.setProperty(prop, value)
        if polish:
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def set_status(self, status: str, frac: float, polish: bool = True) -> None:
        if status == "completed":
            self.bar.hide()
            self.percent.setText("Finished")
            self.percent.setToolTip("")
            self._apply_state(self.percent, "state", "finished", polish)
        elif status in ("failed", "verification_failed"):
            self.bar.hide()
            self.percent.setText("Failed")
            self.percent.setToolTip("")
            self._apply_state(self.percent, "state", "failed", polish)
        else:
            self.bar.show()
            bar_state = "paused" if status == "paused" else "downloading"
            self.bar.set_state(bar_state)
            self.bar.setValue(int(frac * 100))
            self.percent.setText(f"{int(frac * 100)}%")
            self._apply_state(self.percent, "state", "", polish)

    def set_error(self, message: str, polish: bool = True) -> None:
        self.bar.hide()
        self.percent.setText(message)
        self.percent.setToolTip(message)
        self._apply_state(self.percent, "state", "failed", polish)


def _format_added(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = _dt.datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


class DownloadsPage(Page):
    """Table of downloads; ``completed_only`` filters to finished items."""

    def __init__(self, context, *, completed_only: bool = False, parent=None) -> None:
        super().__init__(context, parent)
        self.completed_only = completed_only
        self.filter_type: str | None = "all"
        self.status_filter: str | None = None
        self._rows: dict[int, int] = {}
        # Suppresses the per-row O(n) state scans while a bulk load runs.
        self._bulk = False
        self._progress_timer = None
        self._progress_index = 0
        self._progress_total = 0
        self._ids: list[int] = []
        self._snapshots: dict[int, dict] = {}
        self._last_progress: dict[int, float] = {}
        # Whether events have changed the table since the last full refresh.
        # The page stays live-subscribed while hidden, so ``refresh()`` only
        # needs to run on nav when this is set (or until first build).
        self._dirty = True
        self._empty_row: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(12)

        outer.addLayout(self._build_toolbar())

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setObjectName("downloads_table")
        self.table.setHorizontalHeaderLabels(list(_HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(60)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 110)
        self.table.setColumnWidth(6, 160)

        outer.addWidget(self.table, 1)

        events = context.events
        events.connect(Events.DOWNLOAD_ADDED, self._on_added)
        events.connect(Events.DOWNLOAD_UPDATED, self._on_updated)
        events.connect(Events.DOWNLOAD_REMOVED, self._on_removed)
        events.connect(Events.PROGRESS_UPDATED, self._on_progress)

        self.refresh()

    # ----- construction -----

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.select_all_button = self._tool("select_all", "Select All")
        self.select_all_button.clicked.connect(self._toggle_select_all)
        self.add_button = self._tool("add", "Add")
        self.add_button.setEnabled(True)
        self.add_button.clicked.connect(self._on_add_clicked)
        self.start_button = self._tool("start", "Start")
        self.start_button.clicked.connect(self._start_selected)
        self.pause_button = self._tool("pause", "Pause")
        self.pause_button.clicked.connect(self._pause_selected)
        self.remove_button = self._tool("remove", "Remove")
        self.remove_button.setProperty("role", "danger")
        self.remove_button.clicked.connect(self._remove_selected)

        row.addWidget(self.select_all_button)
        row.addWidget(self.add_button)
        row.addWidget(self.start_button)
        row.addWidget(self.pause_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        return row

    def _tool(self, name: str, text: str):
        button = VerticalIconButton(tool_icon(name), text)
        button.setObjectName("tool_button")
        button.setProperty("tint", name)
        button.setEnabled(False)
        return button

    # ----- filtering -----

    def set_filter(self, category: str | None) -> None:
        category = category or "all"
        if self.filter_type == category and self.status_filter is None:
            # Same category and no pending status filter: events already keep
            # the table live, so re-building it here would be wasted work.
            return
        self.filter_type = category
        self.status_filter = None
        self.refresh()

    def set_status_filter(self, kind: str | None) -> None:
        kind = kind if kind in ("finished", "unfinished") else None
        if self.status_filter == kind and self.filter_type is None:
            return
        self.status_filter = kind
        self.filter_type = None
        self.refresh()

    def _matches(self, snapshot: dict) -> bool:
        if self.completed_only:
            return snapshot.get("status") in TERMINAL
        if self.status_filter == "finished":
            return snapshot.get("status") in TERMINAL
        if self.status_filter == "unfinished":
            return snapshot.get("status") not in TERMINAL
        return snapshot_in_category(snapshot, self.filter_type)

    # ----- selection -----

    def _selected_ids(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                selected.append(self._ids[row])
        return selected

    def _all_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount()) if self.table.item(r, 0) is not None]

    def _toggle_select_all(self) -> None:
        rows = self._all_rows()
        if not rows:
            return
        all_checked = all(self.table.item(r, 0).checkState() == Qt.Checked for r in rows)
        state = Qt.Unchecked if all_checked else Qt.Checked
        self.table.blockSignals(True)
        try:
            for r in rows:
                self.table.item(r, 0).setCheckState(state)
        finally:
            self.table.blockSignals(False)
        self._update_action_states()

    def _update_action_states(self) -> None:
        if self._bulk:
            # These scans are O(rows); running them per row during a bulk load
            # made refresh() quadratic. refresh() re-runs them once at the end.
            return
        has_selection = bool(self._selected_ids())
        for button in (self.start_button, self.pause_button, self.remove_button):
            button.setEnabled(has_selection)
        self.select_all_button.setEnabled(bool(self._all_rows()))

    # ----- actions -----

    def _start_selected(self) -> None:
        manager = self.context.manager
        for download_id in self._selected_ids():
            snapshot = self._snapshots.get(download_id)
            status = snapshot.get("status") if snapshot else ""
            if status == "paused":
                manager.resume(download_id)
            else:
                manager.start(download_id)

    def _pause_selected(self) -> None:
        manager = self.context.manager
        for download_id in self._selected_ids():
            manager.pause(download_id)

    def _remove_selected(self) -> None:
        manager = self.context.manager
        for download_id in self._selected_ids():
            manager.remove(download_id)

    def _on_add_clicked(self) -> None:
        dialog = AddUrlDialog(self.context, parent=self)
        if not dialog.exec():
            return
        if dialog.url().lower().startswith("blob:"):
            self._add_blob_download(dialog)
            return
        from magnetoclip.media.telegram import is_telegram_web_media_url

        if is_telegram_web_media_url(dialog.url()):
            # Telegram Web proxy URLs (e.g. /a/progressive/document...) only work
            # inside the open Telegram tab (they need its session cookie), so
            # they are fetched from the browser instead of downloaded directly.
            self._add_blob_download(dialog)
            return
        if self._offer_playlist(dialog):
            return
        manager = self.context.manager
        quality_kwargs = self._youtube_quality_kwargs(dialog, manager)
        if quality_kwargs is None:
            return
        try:
            download = self._add_with_dedup_dialog(
                manager,
                dialog.url(),
                filename=dialog.filename() or None,
                save_dir=dialog.directory() or None,
                category_name=dialog.category() or None,
                connections_max=dialog.connections() or None,
                proxy_profile_id=dialog.proxy_profile_id(),
                auth_username=dialog.auth_username(),
                auth_password=dialog.auth_password(),
                cookies=dialog.cookies(),
                archive=dialog.archive(),
                **quality_kwargs,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Download", str(exc))
            return
        if not dialog.archive():
            manager.start(download.id)

    def _youtube_quality_kwargs(self, dialog: "AddUrlDialog", manager) -> dict | None:
        """Compute per-download YouTube quality kwargs for ``manager.add``.

        ``None`` means the user cancelled the quality dialog (caller aborts).
        Plain ``{}`` means no quality step applies (non-YouTube, or a concrete
        default from settings that needs no dialog).
        """
        from ..youtube_flow import youtube_quality_kwargs

        return youtube_quality_kwargs(
            self.context,
            dialog.url(),
            cookies=dialog.cookies(),
            parent=self,
        )

    def _offer_playlist(self, dialog: "AddUrlDialog") -> bool:
        """Handle YouTube playlist URLs with the preview dialog.

        Returns ``True`` when the URL was consumed by the playlist flow (either
        the selection was queued, the user cancelled, or a pure playlist URL
        cannot be downloaded as a single video). ``video_with_playlist`` URLs
        that are dismissed fall back to the normal single-video download.
        """
        from magnetoclip.media.youtube.detector import YouTubeURLDetector
        from magnetoclip.media.youtube.types import YouTubeURLType

        url = dialog.url()
        url_type = YouTubeURLDetector.classify(url)
        if url_type in (YouTubeURLType.video, YouTubeURLType.unknown):
            return False
        from magnetoclip.ui.dialogs.playlist import PlaylistDialog

        playlist_dialog = PlaylistDialog(
            url,
            context=self.context,
            cookies=dialog.cookies(),
            skipped_ids=self.context.manager.archived_youtube_ids(),
            default_quality=str(
                self.context.manager.settings.get("youtube.default_quality", "best")
                or "best"
            ),
            parent=self,
        )

        consumed = playlist_dialog.exec() == QDialog.Accepted
        if consumed:
            videos = playlist_dialog.selected_videos()
            if videos and playlist_dialog.playlist is not None:
                manager = self.context.manager
                try:
                    created = manager.add_playlist(
                        playlist_dialog.playlist,
                        videos,
                        save_dir=dialog.directory() or None,
                        category_name=dialog.category() or None,
                        connections_max=dialog.connections() or None,
                        cookies=dialog.cookies(),
                        youtube_quality=playlist_dialog.selected_quality(),
                    )
                except ValueError as exc:
                    QMessageBox.warning(
                        self, "Cannot Add Playlist", str(exc)
                    )
                    return True
                for _, download in created:
                    manager.start(download.id)
        if url_type is YouTubeURLType.playlist:
            return True
        return consumed

    def _add_with_dedup_dialog(self, manager, *args, **kwargs):
        """Add synchronously, prompting the user if a duplicate is detected."""
        decision = getattr(self.context, "dedup_decision", None)
        if decision is None:

            def _ask(payload):
                from magnetoclip.ui.dialogs.duplicate_result import (
                    DuplicateResultDialog,
                )

                dlg = DuplicateResultDialog(payload, parent=self)
                if dlg.exec():
                    return dlg.result_action()
                return "skip"

            self.context.dedup_decision = _ask
            try:
                return manager.add(*args, **kwargs)
            finally:
                self.context.dedup_decision = decision
        return manager.add(*args, **kwargs)

    # ----- blob: URL downloads (fetched from the browser) -----

    def _add_blob_download(self, dialog: AddUrlDialog) -> None:
        """Fetch a pasted ``blob:`` URL or Telegram Web proxy link from the browser.

        The request is persisted for the browser-host process, which pushes it
        to the extension. The extension streams the bytes back and the host
        stores them on the same row; we poll until it is ready, errors, or the
        browser never answers.
        """
        from magnetoclip.database.repositories import BrowserRequestRepository

        with self.context.session_factory() as session:
            request = BrowserRequestRepository(session).add(
                "fetch_blob", payload={"url": dialog.url()}
            )
        request_id = request.id

        from magnetoclip.media import telegram as telegram_media

        is_telegram = telegram_media.is_telegram_web_media_url(dialog.url())
        progress = QProgressDialog(
            "Fetching the content from your browser…\nKeep the page you copied "
            + ("it from open." if not is_telegram else "the Telegram tab open."),
            "Cancel",
            0,
            0,
            self,
        )
        progress.setWindowTitle("MagnetoClip")
        progress.setWindowModality(Qt.WindowModal)
        progress.setCancelButtonText("Cancel")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.show()

        deadline = time.monotonic() + 600
        timer = QTimer(self)
        timer.setInterval(250)
        done = {"value": False}

        def poll() -> None:
            if done["value"]:
                return
            if progress.wasCanceled():
                self._expire_blob_request(request_id)
                self._close_progress(progress, timer, done)
                return
            with self.context.session_factory() as session:
                current = BrowserRequestRepository(session).get(request_id)
            if current is None or current.status == "expired":
                self._close_progress(progress, timer, done)
                QMessageBox.warning(
                    self,
                    "Cannot Add Download",
                    "Timed out waiting for the browser. Is the MagnetoClip "
                    "extension connected and the source page still open?",
                )
                return
            if current.status == "error":
                self._close_progress(progress, timer, done)
                message = (
                    (current.result_json or {}).get("message")
                    or "Could not fetch this content from the browser."
                )
                QMessageBox.warning(self, "Cannot Add Download", message)
                return
            if current.status == "ready":
                self._close_progress(progress, timer, done)
                self._finish_blob_download(dialog, current)
                return
            if time.monotonic() >= deadline:
                self._expire_blob_request(request_id)
                self._close_progress(progress, timer, done)
                QMessageBox.warning(
                    self,
                    "Cannot Add Download",
                    "Timed out waiting for the browser. Make sure the "
                    "MagnetoClip extension is connected.",
                )
                return

        timer.timeout.connect(poll)
        timer.start()
        poll()

    def _expire_blob_request(self, request_id: int) -> None:
        from magnetoclip.database.repositories import BrowserRequestRepository

        try:
            with self.context.session_factory() as session:
                BrowserRequestRepository(session).mark_expired(request_id)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    @staticmethod
    def _close_progress(progress, timer: QTimer, done: dict) -> None:
        done["value"] = True
        timer.stop()
        progress.close()

    def _finish_blob_download(self, dialog: AddUrlDialog, request) -> None:
        meta = request.result_json or {}
        external_path = (meta.get("external_path") or "").strip()
        if external_path:
            self._finish_browser_download(dialog, meta, external_path)
            return
        resolved_url = (meta.get("resolved_url") or "").strip()
        if resolved_url:
            self._finish_resolved_telegram_download(dialog, meta, resolved_url)
            return
        try:
            data = base64.b64decode(request.data_base64, validate=True)
        except Exception:  # noqa: BLE001 - corrupt browser data is rejected
            data = None
        if data is None:
            QMessageBox.warning(
                self, "Cannot Add Download", "The content could not be read."
            )
            return
        filename = self._resolve_fetched_filename(dialog, request, data)
        manager = self.context.manager
        try:
            download = manager.add(
                dialog.url(),
                filename=filename,
                save_dir=dialog.directory() or None,
                category_name=dialog.category() or None,
                connections_max=dialog.connections() or None,
                data=data,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Download", str(exc))
            return
        manager.start(download.id)

    def _finish_browser_download(
        self,
        dialog: AddUrlDialog,
        meta: dict,
        external_path: str,
    ) -> None:
        """Register a file the browser already downloaded to disk.

        Telegram Web ``progressive``/``stream`` links are one-shot session
        tokens that the app and even page scripts cannot re-request — only the
        browser, with the full session, can fetch them while the video is on
        screen. The finished file is handed back here and imported into the
        download folder.
        """
        import os
        from pathlib import Path

        from magnetoclip.network.content import ensure_extension

        source = Path(external_path)
        if not source.is_file() or source.stat().st_size == 0:
            QMessageBox.warning(
                self, "Cannot Add Download", "The download file is missing or empty."
            )
            return
        mime_type = meta.get("mime_type") or None
        candidate = (dialog.filename() or "").strip()
        if not candidate:
            candidate = (meta.get("filename") or "").strip()
        if not candidate:
            candidate = source.name
        candidate = os.path.basename(candidate)
        head = None
        if not os.path.splitext(candidate)[1]:
            try:
                with source.open("rb") as handle:
                    head = handle.read(512)
            except OSError:  # noqa: S110 - best-effort sniff
                head = None
        filename = ensure_extension(candidate, mime_type, head) or candidate
        manager = self.context.manager
        try:
            download = manager.add(
                meta.get("source_url") or source.name,
                filename=filename,
                save_dir=dialog.directory() or None,
                category_name=dialog.category() or None,
                connections_max=dialog.connections() or None,
                data_path=source,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Download", str(exc))
            return
        manager.start(download.id)

    def _finish_resolved_telegram_download(
        self,
        dialog: AddUrlDialog,
        meta: dict,
        resolved_url: str,
    ) -> None:
        """Start a normal download for a Telegram Web proxy link, resolved to
        its direct CDN file by the browser (which owns the session cookie)."""
        from magnetoclip.network.cookies.jar import parse_cookie_header

        cookies = parse_cookie_header(meta.get("cookies") or "") or None
        headers: dict[str, str] | None = None
        referrer = (meta.get("referrer") or "").strip()
        if referrer:
            headers = {"Referer": referrer}
        filename = self._resolve_url_filename(
            dialog.filename() or None, resolved_url, meta.get("mime_type")
        )
        manager = self.context.manager
        try:
            download = manager.add(
                resolved_url,
                filename=filename,
                save_dir=dialog.directory() or None,
                category_name=dialog.category() or None,
                connections_max=dialog.connections() or None,
                proxy_profile_id=dialog.proxy_profile_id(),
                auth_username=dialog.auth_username(),
                auth_password=dialog.auth_password(),
                headers=headers,
                cookies=cookies,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Download", str(exc))
            return
        manager.start(download.id)

    @staticmethod
    def _resolve_fetched_filename(
        dialog: AddUrlDialog, request, data: bytes
    ) -> str | None:
        """Derive a filename for bytes fetched from the browser.

        Preference order: the user's explicit name, then the name the extension
        guessed (with its extension applied when the guess had none, e.g. a
        Telegram ``/a/progressive/document...`` proxy link), then an extension
        recovered from the MIME type or magic bytes.
        """
        from magnetoclip.network.content import blob_extension, ensure_extension

        meta = request.result_json or {}
        mime_type = meta.get("mime_type")
        explicit = dialog.filename()
        if explicit:
            return ensure_extension(explicit, mime_type, data)
        guessed = (meta.get("filename") or "").strip() or None
        if guessed and "." in PurePosixPath(guessed).name:
            return guessed
        ext = blob_extension(meta.get("mime_type"), data)
        if guessed and ext:
            return f"{guessed}.{ext}"
        if ext:
            return f"download.{ext}"
        return guessed

    @staticmethod
    def _resolve_url_filename(
        filename: str | None, resolved_url: str, mime_type: str | None
    ) -> str | None:
        """Ensure a user-typed name for a resolved media URL keeps the format.

        A manual name without an extension (e.g. ``vid``) would otherwise be
        saved extensionless and become unopenable, so the extension is recovered
        from the resolved CDN file's URL path or its MIME type.
        """
        candidate = (filename or "").strip()
        if not candidate:
            return None
        if "." in candidate:
            return candidate
        ext = ""
        if resolved_url:
            tail = PurePosixPath(urlsplit(resolved_url).path or "").name
            if "." in tail:
                ext = tail.rpartition(".")[2]
        if not ext and mime_type:
            mime = PurePath(mime_type).suffix.lstrip(".")
            if mime:
                ext = mime
        if ext:
            candidate = f"{candidate}.{ext}"
        return candidate or None

    def _on_double_clicked(self, row: int, _column: int) -> None:
        download_id = self._ids[row]
        snapshot = self._snapshots.get(download_id)
        if snapshot is None:
            return
        dialog = DownloadDetailsDialog(self.context, snapshot, parent=self.window())
        dialog.exec()

    # ----- context menu -----

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self._ids):
            return
        download_id = self._ids[row]
        menu = self._build_context_menu(download_id)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _build_context_menu(self, download_id: int) -> QMenu:
        snapshot = self._snapshots.get(download_id) or {}
        status = snapshot.get("status") or "queued"
        save_path = snapshot.get("save_path")
        file_exists = bool(save_path and Path(save_path).is_file())

        menu = QMenu(self.table)
        active = status in ACTIVE
        if status == "completed":
            open_file = menu.addAction("Open File")
            open_file.setEnabled(file_exists)
            open_file.triggered.connect(
                lambda: self._open_saved(save_path)
            )
            open_location = menu.addAction("Open File Location")
            open_location.setEnabled(bool(save_path))
            open_location.triggered.connect(
                lambda: self._reveal_saved(save_path)
            )
            preview = menu.addAction("Preview")
            preview.setEnabled(self._can_preview(snapshot))
            preview.triggered.connect(
                lambda: self._preview_saved(download_id)
            )
            menu.addSeparator()
            menu.addAction("Restart Download").triggered.connect(
                lambda: self._restart_download(download_id)
            )
        elif status in ("failed", "verification_failed"):
            menu.addAction("Retry Download").triggered.connect(
                lambda: self._restart_download(download_id)
            )
        elif active:
            menu.addAction("Pause").triggered.connect(
                lambda: self._pause_selected_for(download_id)
            )
        elif status == "paused":
            menu.addAction("Resume").triggered.connect(
                lambda: self._start_selected_for(download_id)
            )
        else:
            menu.addAction("Start").triggered.connect(
                lambda: self._start_selected_for(download_id)
            )

        copy_url = menu.addAction("Copy URL")
        copy_url.triggered.connect(
            lambda: QApplication.clipboard().setText(snapshot.get("url") or "")
        )
        menu.addSeparator()
        remove = menu.addAction("Remove from List")
        remove.triggered.connect(lambda: self._remove_for(download_id))
        return menu

    def _open_saved(self, save_path: str | None) -> None:
        if save_path and Path(save_path).is_file():
            open_path(Path(save_path))

    def _can_preview(self, snapshot) -> bool:
        if not snapshot.get("save_path"):
            return False
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return False
        download = manager.get_download(snapshot["id"])
        if download is None:
            return False
        # Fast path: the resolver recognises the stored path.
        preview = getattr(self.context, "preview", None)
        if preview is not None and preview.type_of_download(download) != "none":
            return True
        # Fallback: the file exists on disk even if the extension is
        # unrecognised — allow the dialog to try opening it externally.
        return Path(snapshot["save_path"]).is_file()

    def _preview_saved(self, download_id: int) -> None:
        from magnetoclip.services.preview.resolver import PreviewType

        from ..dialogs.preview import PreviewDialog

        preview = getattr(self.context, "preview", None)
        manager = getattr(self.context, "manager", None)
        if preview is None or manager is None:
            return
        download = manager.get_download(download_id)
        if download is None:
            return
        path = self._first_existing_path(download)
        if not path:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self.window(),
                "Preview",
                "The file no longer exists on disk.",
            )
            return
        preview_type = preview.resolve(path)
        if preview_type == PreviewType.NONE:
            # File exists but the resolver doesn't recognise the extension —
            # offer to open it with the system default application instead.
            from ..util import open_path

            open_path(Path(path))
            return
        dialog = PreviewDialog(self.context, path, preview_type, parent=self.window())
        dialog.exec()

    def _first_existing_path(self, download) -> str | None:
        preview = getattr(self.context, "preview", None)
        if preview is not None:
            for candidate in preview.filenames(download):
                if candidate and Path(candidate).is_file():
                    return candidate
        media = (getattr(download, "media_metadata_json", None) or {})
        for candidate in (
            getattr(download, "save_path", None),
            media.get("path"),
        ):
            if candidate and Path(candidate).is_file():
                return candidate
        # Fallback: search the parent directory for a file matching the
        # download's filename.  The stored save_path can drift from the
        # actual on-disk name after sanitization or engine reconciliation.
        save_path = getattr(download, "save_path", None)
        if save_path:
            target = Path(save_path)
            parent = target.parent
            stem = target.stem.lower()
            if parent.is_dir():
                for child in parent.iterdir():
                    if child.is_file() and child.stem.lower().startswith(stem):
                        return str(child)
        return None

    def _reveal_saved(self, save_path: str | None) -> None:
        if save_path:
            reveal_path(Path(save_path))

    def _start_selected_for(self, download_id: int) -> None:
        manager = self.context.manager
        snapshot = self._snapshots.get(download_id)
        status = snapshot.get("status") if snapshot else ""
        if status == "paused":
            manager.resume(download_id)
        else:
            manager.start(download_id)

    def _pause_selected_for(self, download_id: int) -> None:
        self.context.manager.pause(download_id)

    def _remove_for(self, download_id: int) -> None:
        self.context.manager.remove(download_id)

    def _restart_download(self, download_id: int) -> None:
        import asyncio

        manager = getattr(self.context, "manager", None)
        if manager is None or not hasattr(manager, "restart"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(manager.restart(download_id))

    # ----- events -----

    def _on_added(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("id"):
            self._upsert(payload)

    def _on_updated(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("id"):
            self._upsert(payload)

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        download_id = payload["id"]
        now = time.monotonic()
        if now - self._last_progress.get(download_id, 0.0) < _UI_PROGRESS_THROTTLE:
            return
        self._last_progress[download_id] = now
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        download = manager.get_download(download_id)
        if download is not None:
            self._upsert(manager.snapshot_item(download))

    def _on_removed(self, payload: Any) -> None:
        download_id = payload.get("id") if isinstance(payload, dict) else payload
        self._remove_row(download_id)
        self._update_empty_state()
        self._update_action_states()

    # ----- row management -----

    def _upsert(self, snapshot: dict, at_top: bool = True) -> None:
        download_id = snapshot["id"]
        self._dirty = True
        if not self._matches(snapshot):
            self._remove_row(download_id)
            self._update_empty_state()
            return
        self._clear_empty_row()
        row = self._rows.get(download_id)
        if row is None:
            if at_top:
                # New downloads land at the top of the table. Existing rows
                # shift down, so every known id must move with its row.
                self.table.insertRow(0)
                row = 0
                for did, existing in list(self._rows.items()):
                    self._rows[did] = existing + 1
                self._ids.insert(0, download_id)
            else:
                # Initial load builds in creation order (newest first).
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._ids.append(download_id)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem())
            self.table.setItem(row, 2, QTableWidgetItem())
            self.table.setItem(row, 4, QTableWidgetItem())
            self.table.setItem(row, 5, QTableWidgetItem())
            self.table.setItem(row, 6, QTableWidgetItem())
            self.table.setCellWidget(row, 3, StatusCell())
            self._rows[download_id] = row
        self._snapshots[download_id] = snapshot
        self._populate_row(row, snapshot)
        self._update_empty_state()

    def _populate_row(self, row: int, snapshot: dict) -> None:
        status = snapshot.get("status") or "queued"
        filename = snapshot.get("filename") or "Untitled"

        name_item = self.table.item(row, 1)
        name_item.setText(filename)
        name_item.setIcon(type_icon(snapshot.get("detected_type")))
        name_item.setToolTip(snapshot.get("url") or filename)
        self._name_quality_suffix(name_item, snapshot)

        downloaded = snapshot.get("size_downloaded") or 0
        total = snapshot.get("size_total")
        if total:
            self.table.item(row, 2).setText(
                f"{format_bytes(downloaded)} / {format_bytes(total)}"
            )
        else:
            self.table.item(row, 2).setText(format_bytes(downloaded))

        speed = snapshot.get("speed")
        cell = self.table.cellWidget(row, 3)
        if isinstance(cell, StatusCell):
            # Freshly built rows are not yet polished, so their style resolves
            # correctly on first paint; re-polishing every one of them during a
            # bulk load dominated the refresh time.
            polish = not self._bulk
            if status in ("failed", "verification_failed"):
                error = snapshot.get("error") or "Failed"
                cell.set_error(error, polish=polish)
            else:
                cell.set_status(status, fraction(downloaded, total), polish=polish)

        if status in ACTIVE:
            self.table.item(row, 4).setText(format_speed(speed))
            eta = snapshot.get("eta_seconds")
            if eta is None and total and speed:
                eta = (total - downloaded) / speed
            self.table.item(row, 5).setText(format_eta(eta))
        elif status == "completed":
            self.table.item(row, 4).setText("Done")
            self.table.item(row, 5).setText("—")
        elif status in ("failed", "verification_failed"):
            error = snapshot.get("error") or "Failed"
            self.table.item(row, 4).setText(
                error if len(error) <= 28 else error[:25] + "…"
            )
            self.table.item(row, 5).setText("—")
        else:
            self.table.item(row, 4).setText("—")
            self.table.item(row, 5).setText("—")

        self.table.item(row, 6).setText(_format_added(snapshot.get("created_at")))

    def _name_quality_suffix(self, name_item, snapshot: dict) -> None:
        """Append the requested/actual YouTube quality to the name cell."""
        requested = snapshot.get("youtube_requested_quality")
        actual_height = snapshot.get("actual_height")
        if not requested and not actual_height:
            return
        from magnetoclip.media.youtube.formats import display_height

        label = requested or "?"
        if actual_height:
            label += f" \u2192 {display_height(actual_height)}"
        name_item.setText(f"{name_item.text()}  \u00b7 {label}")

    def _remove_row(self, download_id: int) -> None:
        self._dirty = True
        row = self._rows.pop(download_id, None)
        if row is None:
            return
        self._snapshots.pop(download_id, None)
        self.table.removeRow(row)
        self._ids.pop(row)
        for did, r in list(self._rows.items()):
            if r > row:
                self._rows[did] = r - 1

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        self._update_action_states()

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column == 0:
            return
        item = self.table.item(row, 0)
        if item is not None and item.checkState() != Qt.Checked:
            item.setCheckState(Qt.Checked)
        self.table.selectRow(row)

    def _clear_empty_row(self) -> None:
        if self._empty_row is None:
            return
        self.table.removeRow(self._empty_row)
        self._empty_row = None

    def _update_empty_state(self) -> None:
        if self._bulk:
            # Recomputed once when the bulk load finishes.
            return
        if self.table.rowCount() == 0:
            self.table.insertRow(0)
            self._empty_row = 0
            self.table.setSpan(0, 1, 1, 6)
            item = QTableWidgetItem("No downloads yet.")
            item.setFlags(Qt.NoItemFlags)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(0, 1, item)
        else:
            self._clear_empty_row()

    def refresh(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        snapshots = list(manager.list_snapshots(limit=2000))
        self._cancel_progressive_load()
        self._rows.clear()
        self._ids.clear()
        self._snapshots.clear()
        self.table.blockSignals(True)
        self._bulk = True
        try:
            self.table.setRowCount(0)
            self._empty_row = None
            if len(snapshots) > _PROGRESSIVE_THRESHOLD:
                # A long history takes seconds to build, and doing that in one
                # blocking pass leaves the page a black rectangle. Hand it to
                # the progressive loader so rows appear while it works.
                self._bulk = False
                self._dirty = False
                self._start_progressive_load(snapshots)
                return
            for snapshot in snapshots:
                self._upsert(snapshot, at_top=False)
        finally:
            self._bulk = False
            # itemChanged fires per populated cell; the bulk fill is
            # programmatic, so the handlers are not interested in it.
            self.table.blockSignals(False)
        self._dirty = False
        self._update_empty_state()
        self._update_action_states()

    # ----- progressive bulk load -----

    def needs_refresh(self) -> bool:
        """True when the table changed since the last full refresh."""
        return self._dirty

    def _start_progressive_load(self, snapshots: list[dict]) -> None:
        """Fill a long history in chunks, keeping the UI painting between them."""
        pending = list(snapshots)
        self._progress_index = 0
        self._progress_total = len(pending)

        # A placeholder, so the table is never a blank rectangle while it fills.
        self.table.blockSignals(True)
        self.table.setRowCount(1)
        self._empty_row = 0
        item = QTableWidgetItem(f"Loading {self._progress_total} downloads…")
        item.setFlags(Qt.NoItemFlags)
        item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(0, 1, item)
        self.table.blockSignals(False)

        timer = QTimer(self)
        timer.timeout.connect(lambda: self._load_chunk(timer, pending))
        self._progress_timer = timer
        timer.start(0)

    def _load_chunk(self, timer: QTimer, pending: list[dict]) -> None:
        if not pending:
            timer.stop()
            self._progress_timer = None
            self._dirty = False
            self._update_empty_state()
            self._update_action_states()
            return

        # Keep each slice short enough to stay well inside a single frame.
        budget_ms = 12
        started = time.perf_counter()
        self.table.blockSignals(True)
        self._bulk = True
        try:
            self._clear_empty_row()
            while pending:
                self._upsert(pending.pop(0), at_top=False)
                self._progress_index += 1
                if (time.perf_counter() - started) * 1000 >= budget_ms:
                    break
        finally:
            self._bulk = False
            self.table.blockSignals(False)

    def _cancel_progressive_load(self) -> None:
        timer = getattr(self, "_progress_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._progress_timer = None
        self._progress_index = 0
        self._progress_total = 0
