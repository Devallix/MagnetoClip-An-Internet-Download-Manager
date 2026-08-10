"""Queue management page: named queues with concurrency limits."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events

from ..components.buttons import VerticalIconButton
from ..components.icons import tool_icon
from ..dialogs.queue import QueueDialog
from .base import Page

ACTIVE_STATUSES = {"connecting", "downloading", "retrying", "verifying"}
PENDING_STATUSES = {"queued", "scheduled"}

_HEADERS = ("", "Queue", "Status", "Max concurrent", "Active", "Pending", "Total")


class QueuePage(Page):
    """Table of download queues; double-click a row to edit its settings."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self._rows: dict[int, int] = {}
        self._ids: list[int] = []
        self._empty_row: int | None = None
        self._download_statuses: dict[int, str] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(12)

        outer.addLayout(self._build_header())
        outer.addLayout(self._build_toolbar())

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setObjectName("downloads_table")
        self.table.setHorizontalHeaderLabels(list(_HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(60)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(5, 80)
        self.table.setColumnWidth(6, 80)

        outer.addWidget(self.table, 1)

        events = context.events
        events.connect(Events.QUEUE_ADDED, lambda _: self.refresh())
        events.connect(Events.QUEUE_REMOVED, lambda _: self.refresh())
        events.connect(Events.QUEUE_UPDATED, lambda _: self.refresh())
        events.connect(Events.DOWNLOAD_ADDED, lambda _: self.refresh())
        events.connect(Events.DOWNLOAD_REMOVED, lambda _: self.refresh())
        events.connect(Events.DOWNLOAD_UPDATED, self._on_download_updated)

        self.refresh()

    # ----- construction -----

    def _build_header(self):
        row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Queue")
        title.setObjectName("page_title")
        subtitle = QLabel("Organize downloads into queues with concurrency limits.")
        subtitle.setObjectName("page_subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        row.addLayout(titles)
        row.addStretch(1)
        return row

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.add_button = self._tool("add", "Add Queue")
        self.add_button.setEnabled(True)
        self.add_button.clicked.connect(self._add_queue)
        self.start_button = self._tool("start", "Start Queue")
        self.start_button.clicked.connect(self._start_selected)
        self.pause_button = self._tool("pause", "Pause Queue")
        self.pause_button.clicked.connect(self._pause_selected)
        self.remove_button = self._tool("remove", "Remove Queue")
        self.remove_button.setProperty("role", "danger")
        self.remove_button.clicked.connect(self._remove_selected)

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

    # ----- selection -----

    def _selected_ids(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                selected.append(self._ids[row])
        return selected

    def _update_action_states(self) -> None:
        has_selection = bool(self._selected_ids())
        for button in (self.start_button, self.pause_button, self.remove_button):
            button.setEnabled(has_selection)

    # ----- actions -----

    def _add_queue(self) -> None:
        dialog = QueueDialog(parent=self)
        if not dialog.exec():
            return
        try:
            self.context.queues.add(dialog.name(), max_concurrent=dialog.max_concurrent())
        except Exception:  # noqa: BLE001 - surface failures in a dialog
            QMessageBox.warning(self, "Queue", "Could not create the queue.")
            return
        self.refresh()

    def _edit_queue(self, row: int) -> None:
        queue_id = self._ids[row]
        queue = self.context.queues.get(queue_id)
        if queue is None:
            return
        dialog = QueueDialog(queue=queue, parent=self)
        if not dialog.exec():
            return
        try:
            self.context.queues.update(
                queue_id,
                name=dialog.name(),
                max_concurrent=dialog.max_concurrent(),
            )
        except Exception:  # noqa: BLE001 - surface failures in a dialog
            QMessageBox.warning(self, "Queue", "Could not update the queue.")

    def _start_selected(self) -> None:
        for queue_id in self._selected_ids():
            self.context.queues.advance(queue_id)

    def _pause_selected(self) -> None:
        manager = self.context.manager
        for queue_id in self._selected_ids():
            for item in self.context.queues.items(queue_id):
                download = item.download
                if download is not None and download.status in ACTIVE_STATUSES:
                    manager.pause(download.id)

    def _remove_selected(self) -> None:
        for queue_id in self._selected_ids():
            try:
                self.context.queues.remove(queue_id)
            except KeyError:
                pass

    def _on_double_clicked(self, row: int, _column: int) -> None:
        if self._empty_row is not None and row == self._empty_row:
            return
        self._edit_queue(row)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        self._update_action_states()

    # ----- events -----

    def _on_download_updated(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        download_id = payload.get("id")
        status = payload.get("status")
        if download_id is None or status is None:
            return
        if self._download_statuses.get(download_id) != status:
            self._download_statuses[download_id] = status
            self.refresh()

    # ----- row management -----

    def _clear_empty_row(self) -> None:
        if self._empty_row is None:
            return
        self.table.removeRow(self._empty_row)
        self._empty_row = None

    def _upsert(self, queue_id: int, queue) -> None:
        row = self._rows.get(queue_id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, check)
            for column in range(1, len(_HEADERS)):
                self.table.setItem(row, column, QTableWidgetItem())
            self._rows[queue_id] = row
            self._ids.append(queue_id)
        self._populate_row(row, queue)
        self._update_empty_state()

    def _populate_row(self, row: int, queue) -> None:
        self.table.item(row, 1).setText(queue.name)

        downloads = [
            item.download
            for item in self.context.queues.items(queue.id)
            if item.download is not None
        ]
        active = sum(1 for dl in downloads if dl.status in ACTIVE_STATUSES)
        pending = sum(1 for dl in downloads if dl.status in PENDING_STATUSES)

        status = "Running" if active else ("Queued" if pending else "Idle")
        self.table.item(row, 2).setText(status)
        self.table.item(row, 3).setText(str(queue.max_concurrent))
        self.table.item(row, 4).setText(str(active))
        self.table.item(row, 5).setText(str(pending))
        self.table.item(row, 6).setText(str(len(downloads)))

    def _update_empty_state(self) -> None:
        if self.table.rowCount() == 0:
            self.table.insertRow(0)
            self._empty_row = 0
            self.table.setSpan(0, 1, 1, 6)
            item = QTableWidgetItem("No queues yet. Add a queue to organize your downloads.")
            item.setFlags(Qt.NoItemFlags)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(0, 1, item)
        else:
            self._clear_empty_row()

    def refresh(self) -> None:
        queues = getattr(getattr(self.context, "queues", None), "list", list)()
        known_ids: set[int] = set()
        for queue in queues:
            for item in self.context.queues.items(queue.id):
                if item.download is not None:
                    known_ids.add(item.download.id)
        self._download_statuses = {
            download_id: status
            for download_id, status in self._download_statuses.items()
            if download_id in known_ids
        }

        self._rows.clear()
        self._ids.clear()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._empty_row = None
        self.table.blockSignals(False)
        for queue in queues:
            self._upsert(queue.id, queue)
        self._update_empty_state()
        self._update_action_states()
