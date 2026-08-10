"""Create / edit bandwidth schedule dialog: time window, days, and speeds."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTimeEdit,
    QVBoxLayout,
)

from ...database.models import Schedule

_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class ScheduleDialog(QDialog):
    def __init__(self, schedule: Schedule | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Schedule" if schedule is not None else "New Schedule")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Night bandwidth")
        if schedule is not None:
            self.name_edit.setText(schedule.name)
        layout.addWidget(self.name_edit)

        self.all_day_check = QCheckBox("All day (no time window)")
        self.all_day_check.setChecked(True)
        layout.addWidget(self.all_day_check)

        window_row = QHBoxLayout()
        window_row.addWidget(QLabel("From"))
        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm")
        self.start_edit.setTime(self.start_edit.time().fromString("22:00", "HH:mm"))
        window_row.addWidget(self.start_edit)
        window_row.addWidget(QLabel("to"))
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm")
        self.end_edit.setTime(self.end_edit.time().fromString("06:00", "HH:mm"))
        window_row.addWidget(self.end_edit)
        window_row.addStretch(1)
        layout.addLayout(window_row)

        layout.addWidget(QLabel("Days"))
        days_row = QHBoxLayout()
        self.day_checks: list[QCheckBox] = []
        for i, label in enumerate(_DAY_LABELS):
            check = QCheckBox(label)
            check.setChecked(True)
            self.day_checks.append(check)
            days_row.addWidget(check)
        days_row.addStretch(1)
        layout.addLayout(days_row)

        speeds = QGridLayout()
        speeds.setHorizontalSpacing(8)
        speeds.addWidget(QLabel("Day speed (MB/s)"), 0, 0)
        self.speed_day_spin = QDoubleSpinBox()
        self.speed_day_spin.setRange(0, 10000)
        self.speed_day_spin.setDecimals(1)
        self.speed_day_spin.setSpecialValueText("Unlimited")
        speeds.addWidget(self.speed_day_spin, 0, 1)
        speeds.addWidget(QLabel("Night speed (MB/s)"), 1, 0)
        self.speed_night_spin = QDoubleSpinBox()
        self.speed_night_spin.setRange(0, 10000)
        self.speed_night_spin.setDecimals(1)
        self.speed_night_spin.setSpecialValueText("Unlimited")
        speeds.addWidget(self.speed_night_spin, 1, 1)
        speeds.setColumnStretch(1, 1)
        layout.addLayout(speeds)

        self.enabled_check = QCheckBox("Enabled")
        layout.addWidget(self.enabled_check)

        if schedule is not None:
            self._populate(schedule)

        self.all_day_check.toggled.connect(self.start_edit.setDisabled)
        self.all_day_check.toggled.connect(self.end_edit.setDisabled)
        self.start_edit.setEnabled(not self.all_day_check.isChecked())
        self.end_edit.setEnabled(not self.all_day_check.isChecked())

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate(self, schedule: Schedule) -> None:
        has_window = schedule.start_time is not None or schedule.end_time is not None
        self.all_day_check.setChecked(not has_window)
        if has_window:
            start = schedule.start_time or "22:00"
            end = schedule.end_time or "06:00"
            self.start_edit.setTime(self.start_edit.time().fromString(start, "HH:mm"))
            self.end_edit.setTime(self.end_edit.time().fromString(end, "HH:mm"))
        for i, check in enumerate(self.day_checks):
            check.setChecked(bool(schedule.days_mask & (1 << i)))
        if schedule.speed_day:
            self.speed_day_spin.setValue(schedule.speed_day)
        if schedule.speed_night:
            self.speed_night_spin.setValue(schedule.speed_night)
        self.enabled_check.setChecked(schedule.enabled)

    def _validate_and_accept(self) -> None:
        if self.name_edit.text().strip() and any(c.isChecked() for c in self.day_checks):
            self.accept()

    def name(self) -> str:
        return self.name_edit.text().strip()

    def start_time(self) -> str | None:
        if self.all_day_check.isChecked():
            return None
        return self.start_edit.time().toString("HH:mm")

    def end_time(self) -> str | None:
        if self.all_day_check.isChecked():
            return None
        return self.end_edit.time().toString("HH:mm")

    def days_mask(self) -> int:
        mask = 0
        for i, check in enumerate(self.day_checks):
            if check.isChecked():
                mask |= 1 << i
        return mask

    def speed_day(self) -> float | None:
        value = self.speed_day_spin.value()
        return value if value > 0 else None

    def speed_night(self) -> float | None:
        value = self.speed_night_spin.value()
        return value if value > 0 else None

    def enabled(self) -> bool:
        return self.enabled_check.isChecked()
