"""Downloads page: a table of downloads with per-row progress and controls."""

from __future__ import annotations

import base64
import datetime as _dt
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.core.events.bus import Events

from ..categories import snapshot_in_category
from ..components.buttons import VerticalIconButton
from ..components.icons import tool_icon, type_icon
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


class StatusCell(QWidget):
    """Status column: a state-colored progress bar plus a percent label."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 10, 4)
        layout.setSpacing(8)

        self.bar = QProgressBar()
        self.bar.setObjectName("status_bar")
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.percent = QLabel("0%")
        self.percent.setObjectName("status_percent")

        layout.addWidget(self.bar, 1)
        layout.addWidget(self.percent)

    def set_status(self, status: str, frac: float) -> None:
        if status == "completed":
            self.bar.hide()
            self.percent.setText("Finished")
            self.percent.setToolTip("")
            self.percent.setProperty("state", "finished")
        elif status in ("failed", "verification_failed"):
            self.bar.hide()
            self.percent.setText("Failed")
            self.percent.setToolTip("")
            self.percent.setProperty("state", "failed")
        else:
            self.bar.show()
            bar_state = "paused" if status == "paused" else "downloading"
            self.bar.setProperty("state", bar_state)
            self.bar.style().unpolish(self.bar)
            self.bar.style().polish(self.bar)
            self.bar.setValue(int(frac * 100))
            self.percent.setText(f"{int(frac * 100)}%")
            self.percent.setProperty("state", "")
        self.percent.style().unpolish(self.percent)
        self.percent.style().polish(self.percent)

    def set_error(self, message: str) -> None:
        self.bar.hide()
        self.percent.setText(message)
        self.percent.setToolTip(message)
        self.percent.setProperty("state", "failed")
        self.percent.style().unpolish(self.percent)
        self.percent.style().polish(self.percent)


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
        self._ids: list[int] = []
        self._snapshots: dict[int, dict] = {}
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
        self.filter_type = category or "all"
        self.status_filter = None
        self.refresh()

    def set_status_filter(self, kind: str | None) -> None:
        self.status_filter = kind if kind in ("finished", "unfinished") else None
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
        manager = self.context.manager
        try:
            download = manager.add(
                dialog.url(),
                filename=dialog.filename() or None,
                save_dir=dialog.directory() or None,
                category_name=dialog.category() or None,
                queue_id=dialog.queue_id(),
                connections_max=dialog.connections() or None,
                proxy_profile_id=dialog.proxy_profile_id(),
                auth_username=dialog.auth_username(),
                auth_password=dialog.auth_password(),
                cookies=dialog.cookies(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Download", str(exc))
            return
        manager.start(download.id)

    # ----- blob: URL downloads (fetched from the browser) -----

    def _add_blob_download(self, dialog: AddUrlDialog) -> None:
        """Fetch a pasted ``blob:`` URL from the browser via the extension.

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

        progress = QProgressDialog(
            "Fetching blob from your browser…\nKeep the page you copied it "
            "from open.",
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

        deadline = time.monotonic() + 30
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
                    or "Could not fetch this blob from the browser."
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
        try:
            data = base64.b64decode(request.data_base64, validate=True)
        except Exception:  # noqa: BLE001 - corrupt blob data is rejected
            data = None
        if data is None:
            QMessageBox.warning(
                self, "Cannot Add Download", "The blob data could not be read."
            )
            return
        filename = (
            dialog.filename()
            or (request.result_json or {}).get("filename")
            or None
        )
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
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        download = manager.get_download(payload["id"])
        if download is not None:
            self._upsert(manager.snapshot_item(download))

    def _on_removed(self, payload: Any) -> None:
        download_id = payload.get("id") if isinstance(payload, dict) else payload
        self._remove_row(download_id)
        self._update_empty_state()
        self._update_action_states()

    # ----- row management -----

    def _upsert(self, snapshot: dict) -> None:
        download_id = snapshot["id"]
        if not self._matches(snapshot):
            self._remove_row(download_id)
            self._update_empty_state()
            return
        self._clear_empty_row()
        row = self._rows.get(download_id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
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
            self._ids.append(download_id)
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
            if status in ("failed", "verification_failed"):
                error = snapshot.get("error") or "Failed"
                cell.set_error(error)
            else:
                cell.set_status(status, fraction(downloaded, total))

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

    def _remove_row(self, download_id: int) -> None:
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
        self._rows.clear()
        self._ids.clear()
        self._snapshots.clear()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._empty_row = None
        self.table.blockSignals(False)
        for snapshot in manager.list_snapshots(limit=2000):
            self._upsert(snapshot)
        self._update_empty_state()
        self._update_action_states()
