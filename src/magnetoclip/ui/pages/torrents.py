"""Torrents page: add, active torrents, and seeding list.

Behaves like uTorrent:
- Click "Add" to open the dialog blank (paste magnet or URL inside)
- Click "Upload .torrent" to pick a file from disk -> opens info dialog
- Active downloads table with full control toolbar
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.core.events.bus import Events

from ..components.buttons import VerticalIconButton
from ..components.icons import tool_icon
from ..dialogs.add_torrent import AddTorrentDialog
from ..util import format_bytes, format_speed
from .base import Page

ACTIVE = {"connecting", "downloading", "retrying", "verifying"}
TERMINAL = {"completed", "failed", "verification_failed", "stopped"}


class TorrentsPage(Page):
    """Dedicated page for torrent downloads: active and seeding."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self._rows: dict[int, int] = {}
        self._ids: list[int] = []
        self._snapshots: dict[int, dict] = {}
        self._empty_row: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(12)

        # --- Active/Seeding table ---
        active_label = QLabel("Active Downloads")
        active_label.setObjectName("page_subtitle")
        outer.addWidget(active_label)

        toolbar = QHBoxLayout()
        self.add_button = VerticalIconButton(tool_icon("add"), "Add")
        self.add_button.setObjectName("tool_button")
        self.add_button.setProperty("tint", "add")
        self.add_button.clicked.connect(self._on_add_clicked)
        toolbar.addWidget(self.add_button)

        self.upload_button = VerticalIconButton(tool_icon("add"), "Upload")
        self.upload_button.setObjectName("tool_button")
        self.upload_button.setProperty("tint", "upload")
        self.upload_button.clicked.connect(self._on_upload_torrent)
        toolbar.addWidget(self.upload_button)

        toolbar.addSpacing(12)

        self.select_all_button = VerticalIconButton(tool_icon("select_all"), "Select All")
        self.select_all_button.setObjectName("tool_button")
        self.select_all_button.setProperty("tint", "add")
        self.select_all_button.clicked.connect(self._on_select_all)
        toolbar.addWidget(self.select_all_button)

        self.pause_button = VerticalIconButton(tool_icon("pause"), "Pause")
        self.pause_button.setObjectName("tool_button")
        self.pause_button.setProperty("tint", "pause")
        self.pause_button.clicked.connect(self._on_pause_selected)
        toolbar.addWidget(self.pause_button)

        self.resume_button = VerticalIconButton(tool_icon("start"), "Resume")
        self.resume_button.setObjectName("tool_button")
        self.resume_button.setProperty("tint", "start")
        self.resume_button.clicked.connect(self._on_resume_selected)
        toolbar.addWidget(self.resume_button)

        self.delete_button = VerticalIconButton(tool_icon("remove"), "Delete")
        self.delete_button.setObjectName("tool_button")
        self.delete_button.setProperty("tint", "remove")
        self.delete_button.clicked.connect(self._on_delete_selected)
        toolbar.addWidget(self.delete_button)

        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        self.table = QTableWidget(0, 7)
        self.table.setObjectName("torrents_table")
        self.table.setHorizontalHeaderLabels(
            ["", "Name", "Status", "Progress", "Speed", "Peers", "ETA"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.itemSelectionChanged.connect(self._on_active_selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 180)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 80)
        outer.addWidget(self.table, 1)

        events = context.events
        events.connect(Events.DOWNLOAD_ADDED, self._on_added)
        events.connect(Events.DOWNLOAD_UPDATED, self._on_updated)
        events.connect(Events.DOWNLOAD_REMOVED, self._on_removed)
        events.connect(Events.PROGRESS_UPDATED, self._on_progress)

        self.refresh()

    # ----- add routing -----

    def _on_add_clicked(self) -> None:
        self._open_add_dialog(url="")

    def _on_upload_torrent(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open .torrent File", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        if not path:
            return
        self._open_add_dialog(url=path)

    def _open_add_dialog(self, *, url: str = "") -> None:
        dialog = AddTorrentDialog(self.context, parent=self, url=url)
        if not dialog.exec():
            return
        self._commit_download(dialog)

    def _commit_download(self, dialog: AddTorrentDialog) -> None:
        manager = self.context.manager
        try:
            kwargs: dict[str, Any] = {
                "url": dialog.url(),
                "category_name": dialog.category() or None,
                "queue_id": dialog.queue_id(),
            }
            if dialog.filename():
                kwargs["filename"] = dialog.filename()
            if dialog.directory():
                kwargs["save_dir"] = dialog.directory()
            download = manager.add(**kwargs)

            from magnetoclip.torrent.detect import is_torrent_url

            if not is_torrent_url(download.url):
                with manager.session_factory() as session:
                    from magnetoclip.database.repositories import DownloadRepository

                    rec = DownloadRepository(session).get(download.id)
                    if rec is not None:
                        rec.detected_type = "torrent"
                        session.commit()

            if is_torrent_url(download.url):
                manager._pending_torrent_opts[download.id] = {
                    "sequential": dialog.sequential(),
                    "seed_mode": dialog.seed_mode(),
                }
                with manager.session_factory() as session:
                    from magnetoclip.database.repositories import DownloadRepository

                    rec = DownloadRepository(session).get(download.id)
                    if rec is not None:
                        rec.torrent_sequential = dialog.sequential()
                        session.commit()

            manager.start(download.id)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Torrent", str(exc))

    # ----- active table -----

    def _matches(self, snapshot: dict) -> bool:
        return snapshot.get("detected_type") == "torrent"

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
            if self._matches(snapshot):
                self._upsert(snapshot)
        self._update_empty_state()

    def _upsert(self, snapshot: dict) -> None:
        download_id = snapshot["id"]
        if not self._matches(snapshot):
            self._remove_row(download_id)
            return
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
            self.table.setCellWidget(row, 3, self._make_progress_cell())
            self.table.setItem(row, 4, QTableWidgetItem())
            self.table.setItem(row, 5, QTableWidgetItem())
            self.table.setItem(row, 6, QTableWidgetItem())
            self._rows[download_id] = row
            self._ids.append(download_id)
        self._snapshots[download_id] = snapshot
        self._populate_row(row, snapshot)

    def _make_progress_cell(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(6, 4, 10, 4)
        layout.setSpacing(8)
        bar = QProgressBar()
        bar.setObjectName("status_bar")
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setValue(0)
        percent = QLabel("0%")
        percent.setObjectName("status_percent")
        layout.addWidget(bar, 1)
        layout.addWidget(percent)
        return widget

    def _populate_row(self, row: int, snapshot: dict) -> None:
        status = snapshot.get("status") or "queued"
        filename = snapshot.get("filename") or "Untitled"

        self.table.item(row, 1).setText(filename)
        self.table.item(row, 1).setToolTip(snapshot.get("url") or filename)

        peers = snapshot.get("torrent_num_peers") or 0
        seeds = snapshot.get("torrent_num_seeds") or 0
        self.table.item(row, 5).setText(f"{peers}/{seeds}")

        downloaded = snapshot.get("size_downloaded") or 0
        total = snapshot.get("size_total")
        speed = snapshot.get("speed")

        cell = self.table.cellWidget(row, 3)
        if cell:
            bar = cell.findChild(QProgressBar)
            percent_label = cell.findChild(QLabel)
            if bar and percent_label:
                if total and total > 0:
                    frac = downloaded / total
                    bar.setValue(int(frac * 100))
                    percent_label.setText(f"{int(frac * 100)}%")
                else:
                    bar.setValue(0)
                    percent_label.setText("--")

        if status in ACTIVE:
            self.table.item(row, 2).setText("Downloading")
            self.table.item(row, 4).setText(format_speed(speed))
            eta = snapshot.get("eta_seconds")
            if eta is None and total and speed:
                eta = (total - downloaded) / speed
            from ..util import format_eta

            self.table.item(row, 6).setText(format_eta(eta))
        elif status == "paused":
            self.table.item(row, 2).setText("Paused")
            self.table.item(row, 4).setText("--")
            self.table.item(row, 6).setText("--")
        elif status == "completed":
            seeding = snapshot.get("torrent_seeding", False)
            self.table.item(row, 2).setText("Seeding" if seeding else "Completed")
            self.table.item(row, 4).setText(format_speed(speed))
            self.table.item(row, 6).setText("--")
        elif status in ("failed", "verification_failed"):
            self.table.item(row, 2).setText("Failed")
            self.table.item(row, 4).setText(snapshot.get("error", "Failed")[:20])
            self.table.item(row, 6).setText("--")
        else:
            self.table.item(row, 2).setText(status.capitalize())
            self.table.item(row, 4).setText("--")
            self.table.item(row, 6).setText("--")

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

    def _update_empty_state(self) -> None:
        if self.table.rowCount() == 0:
            self.table.insertRow(0)
            self._empty_row = 0
            self.table.setSpan(0, 1, 1, 6)
            item = QTableWidgetItem("No torrent downloads yet.")
            item.setFlags(Qt.NoItemFlags)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(0, 1, item)
        else:
            if self._empty_row is not None:
                self.table.removeRow(self._empty_row)
                self._empty_row = None

    # ----- toolbar actions -----

    def _checked_download_ids(self) -> list[int]:
        """Return download IDs for rows whose checkbox is checked."""
        ids = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                if row < len(self._ids):
                    ids.append(self._ids[row])
        return ids

    def _on_active_selection_changed(self) -> None:
        """Auto-check/uncheck checkboxes when rows are selected/deselected."""
        selected_rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            if row in selected_rows:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)

    def _on_select_all(self) -> None:
        if self.table.rowCount() == 0:
            return
        real_rows = [r for r in range(self.table.rowCount()) if r != self._empty_row]
        all_checked = all(
            self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.Checked
            for r in real_rows
        )
        state = Qt.Unchecked if all_checked else Qt.Checked
        for r in real_rows:
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(state)
        if state == Qt.Checked:
            self.table.selectAll()
        else:
            self.table.clearSelection()

    def _on_pause_selected(self) -> None:
        manager = self.context.manager
        for did in self._checked_download_ids():
            manager.pause(did)

    def _on_resume_selected(self) -> None:
        manager = self.context.manager
        for did in self._checked_download_ids():
            manager.resume(did)

    def _on_delete_selected(self) -> None:
        manager = self.context.manager
        for did in list(self._checked_download_ids()):
            manager.remove(did)

    # ----- context menu -----

    def _on_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        if row < 0 or row >= len(self._ids):
            return
        download_id = self._ids[row]
        snapshot = self._snapshots.get(download_id) or {}
        status = snapshot.get("status") or "queued"

        menu = QMenu(self.table)
        if status in ACTIVE:
            menu.addAction("Pause").triggered.connect(
                lambda did=download_id: self.context.manager.pause(did)
            )
        elif status == "paused":
            menu.addAction("Resume").triggered.connect(
                lambda did=download_id: self.context.manager.resume(did)
            )
        elif status == "completed":
            menu.addAction("Start Seeding").triggered.connect(
                lambda did=download_id: self.context.manager.start_seeding(did)
            )
        else:
            menu.addAction("Start").triggered.connect(
                lambda did=download_id: self.context.manager.start(did)
            )

        menu.addSeparator()
        menu.addAction("Copy URL").triggered.connect(
            lambda did=download_id: self._copy_url(did)
        )
        remove = menu.addAction("Remove")
        remove.triggered.connect(
            lambda did=download_id: self.context.manager.remove(did)
        )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_url(self, download_id: int) -> None:
        from PySide6.QtWidgets import QApplication

        snapshot = self._snapshots.get(download_id) or {}
        url = snapshot.get("url", "")
        if url:
            QApplication.clipboard().setText(url)

    # ----- events -----

    def _on_added(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("id"):
            if self._matches(payload):
                self._upsert(payload)
                self._update_empty_state()

    def _on_updated(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("id"):
            if self._matches(payload):
                self._upsert(payload)

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        download = manager.get_download(payload["id"])
        if download is not None:
            snapshot = manager.snapshot_item(download)
            if self._matches(snapshot):
                self._upsert(snapshot)

    def _on_removed(self, payload: Any) -> None:
        download_id = payload.get("id") if isinstance(payload, dict) else payload
        self._remove_row(download_id)
        self._update_empty_state()
