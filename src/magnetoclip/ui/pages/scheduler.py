"""Scheduler page: time-window bandwidth schedules with day/night speeds."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events
from magnetoclip.database.repositories import ScheduleRepository, SettingsStore

from ..components.buttons import VerticalIconButton
from ..components.icons import tool_icon
from ..dialogs.schedule import ScheduleDialog
from .base import Page

_HEADERS = ("", "Schedule", "Window", "Days", "Day speed", "Night speed", "Status")
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ALL_DAYS = 0b1111111
_WEEKDAYS = 0b0011111
_WEEKENDS = 0b1100000


class SchedulerPage(Page):
    """Table of bandwidth schedules; double-click a row to edit its settings."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self._rows: dict[int, int] = {}
        self._ids: list[int] = []
        self._empty_row: int | None = None
        self.scheduler = getattr(context, "scheduler", None)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(12)

        outer.addLayout(self._build_header())
        outer.addLayout(self._build_toolbar())

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setObjectName("schedules_table")
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
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 80)

        outer.addWidget(self.table, 1)

        events = context.events
        events.connect(Events.SCHEDULE_ADDED, lambda _: self.refresh())
        events.connect(Events.SCHEDULE_UPDATED, lambda _: self.refresh())
        events.connect(Events.SCHEDULE_REMOVED, lambda _: self.refresh())
        events.connect(Events.SETTINGS_CHANGED, lambda _: self._refresh_master())

        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self._refresh_statuses)
        self.timer.start()

        self.refresh()

    # ----- construction -----

    def _build_header(self):
        row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Scheduler")
        title.setObjectName("page_title")
        subtitle = QLabel("Limit bandwidth by day and time of day.")
        subtitle.setObjectName("page_subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        row.addLayout(titles)
        row.addStretch(1)
        self.enable_check = QCheckBox("Enable scheduler")
        self.enable_check.toggled.connect(self._on_master_toggled)
        row.addWidget(self.enable_check)
        return row

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.add_button = self._tool("add", "Add Schedule")
        self.add_button.setEnabled(True)
        self.add_button.clicked.connect(self._add_schedule)
        self.enable_button = self._tool("start", "Enable Selected")
        self.enable_button.clicked.connect(self._enable_selected)
        self.disable_button = self._tool("pause", "Disable Selected")
        self.disable_button.clicked.connect(self._disable_selected)
        self.remove_button = self._tool("remove", "Remove Schedule")
        self.remove_button.setProperty("role", "danger")
        self.remove_button.clicked.connect(self._remove_selected)

        row.addWidget(self.add_button)
        row.addWidget(self.enable_button)
        row.addWidget(self.disable_button)
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
        for button in (self.enable_button, self.disable_button, self.remove_button):
            button.setEnabled(has_selection)

    # ----- actions -----

    def _add_schedule(self) -> None:
        dialog = ScheduleDialog(parent=self)
        if not dialog.exec():
            return
        try:
            self._repo().add(
                dialog.name(),
                start_time=dialog.start_time(),
                end_time=dialog.end_time(),
                days_mask=dialog.days_mask(),
                speed_day=dialog.speed_day(),
                speed_night=dialog.speed_night(),
                enabled=dialog.enabled(),
            )
        except Exception:  # noqa: BLE001 - surface failures in a dialog
            QMessageBox.warning(self, "Scheduler", "Could not create the schedule.")
            return
        self.context.events.post(Events.SCHEDULE_ADDED, None)

    def _edit_schedule(self, row: int) -> None:
        schedule_id = self._ids[row]
        schedule = self._repo().get(schedule_id)
        if schedule is None:
            return
        dialog = ScheduleDialog(schedule=schedule, parent=self)
        if not dialog.exec():
            return
        try:
            self._repo().update(
                schedule_id,
                name=dialog.name(),
                start_time=dialog.start_time(),
                end_time=dialog.end_time(),
                days_mask=dialog.days_mask(),
                speed_day=dialog.speed_day(),
                speed_night=dialog.speed_night(),
                enabled=dialog.enabled(),
            )
        except Exception:  # noqa: BLE001 - surface failures in a dialog
            QMessageBox.warning(self, "Scheduler", "Could not update the schedule.")
            return
        self.context.events.post(Events.SCHEDULE_UPDATED, schedule_id)
        self._request_apply()

    def _enable_selected(self) -> None:
        for schedule_id in self._selected_ids():
            self._repo().update(schedule_id, enabled=True)
        self.context.events.post(Events.SCHEDULE_UPDATED, None)
        self._request_apply()

    def _disable_selected(self) -> None:
        for schedule_id in self._selected_ids():
            self._repo().update(schedule_id, enabled=False)
        self.context.events.post(Events.SCHEDULE_UPDATED, None)
        self._request_apply()

    def _remove_selected(self) -> None:
        for schedule_id in self._selected_ids():
            schedule = self._repo().get(schedule_id)
            if schedule is not None:
                self._repo().remove(schedule)
        self.context.events.post(Events.SCHEDULE_REMOVED, None)
        self._request_apply()

    def _on_master_toggled(self, checked: bool) -> None:
        self.context.settings.set("scheduler.enabled", checked)
        store = SettingsStore(self.context.session_factory)
        store.save_many(self.context.settings.to_store_dict())
        self.context.events.post(Events.SETTINGS_CHANGED, {"scheduler": True})
        if self.scheduler is not None:
            self.scheduler.request_toggle(checked)

    def _on_double_clicked(self, row: int, _column: int) -> None:
        if self._empty_row is not None and row == self._empty_row:
            return
        self._edit_schedule(row)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        self._update_action_states()

    def _request_apply(self) -> None:
        scheduler = self.scheduler
        if scheduler is not None:
            scheduler.request_toggle(
                bool(self.context.settings.get("scheduler.enabled", False))
            )

    # ----- data access -----

    def _repo(self) -> ScheduleRepository:
        return ScheduleRepository(self.context.new_session())

    # ----- row management -----

    def _clear_empty_row(self) -> None:
        if self._empty_row is None:
            return
        self.table.removeRow(self._empty_row)
        self._empty_row = None

    def _upsert(self, schedule_id: int, schedule) -> None:
        row = self._rows.get(schedule_id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, check)
            for column in range(1, len(_HEADERS)):
                self.table.setItem(row, column, QTableWidgetItem())
            self._rows[schedule_id] = row
            self._ids.append(schedule_id)
        self._populate_row(row, schedule)
        self._update_empty_state()

    def _populate_row(self, row: int, schedule) -> None:
        self.table.item(row, 1).setText(schedule.name)
        self.table.item(row, 2).setText(self._window_text(schedule))
        self.table.item(row, 3).setText(self._days_text(schedule.days_mask))
        self.table.item(row, 4).setText(self._speed_text(schedule.speed_day))
        self.table.item(row, 5).setText(self._speed_text(schedule.speed_night))
        self.table.item(row, 6).setText(self._status_text(schedule))

    @staticmethod
    def _speed_text(speed) -> str:
        return "Unlimited" if not speed else f"{speed:g} MB/s"

    @staticmethod
    def _window_text(schedule) -> str:
        if schedule.start_time is None and schedule.end_time is None:
            return "All day"
        start = schedule.start_time or "00:00"
        end = schedule.end_time or "00:00"
        return f"{start} - {end}"

    @classmethod
    def _days_text(cls, mask: int) -> str:
        if mask == _ALL_DAYS:
            return "Every day"
        if mask == _WEEKDAYS:
            return "Weekdays"
        if mask == _WEEKENDS:
            return "Weekends"
        names = [name for index, name in enumerate(_DAY_NAMES) if mask & (1 << index)]
        return ", ".join(names) if names else "None"

    def _status_text(self, schedule) -> str:
        if not schedule.enabled:
            return "Off"
        scheduler = self.scheduler
        if scheduler is not None and scheduler.is_active(schedule):
            return "Active"
        return "Idle"

    def _update_empty_state(self) -> None:
        if self.table.rowCount() == 0:
            self.table.insertRow(0)
            self._empty_row = 0
            self.table.setSpan(0, 1, 1, 6)
            item = QTableWidgetItem(
                "No schedules yet. Add a schedule to control bandwidth by time of day."
            )
            item.setFlags(Qt.NoItemFlags)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(0, 1, item)
        else:
            self._clear_empty_row()

    # ----- events -----

    def _refresh_master(self) -> None:
        self.enable_check.blockSignals(True)
        self.enable_check.setChecked(
            bool(self.context.settings.get("scheduler.enabled", False))
        )
        self.enable_check.blockSignals(False)

    def _refresh_statuses(self) -> None:
        if self._empty_row is not None:
            return
        for row in range(self.table.rowCount()):
            schedule_id = self._ids[row]
            schedule = self._repo().get(schedule_id)
            if schedule is not None:
                self.table.item(row, 6).setText(self._status_text(schedule))

    def refresh(self) -> None:
        self._refresh_master()
        repo = self._repo()
        schedules = repo.list()

        self._rows.clear()
        self._ids.clear()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._empty_row = None
        self.table.blockSignals(False)
        for schedule in schedules:
            self._upsert(schedule.id, schedule)
        self._update_empty_state()
        self._update_action_states()
