"""Pause page: a simple switch to pause all downloads, with an optional
auto-resume timer.

Replaces the old weekly time-window scheduler with one unambiguous control:
toggle to pause (everything stops immediately), optionally set an auto-resume
countdown, and back on to resume.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events

from .base import Page, make_scrollable


class PausePage(Page):
    """Manage the global pause switch and its auto-resume timer."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self.setObjectName("pause_page")
        layout = make_scrollable(self, margins=(24, 20, 24, 20), spacing=16)

        # ── card: pause switch ──
        card, cl = self._card("Pause Downloads")
        self.pause_check = QCheckBox("Pause all downloads")
        self.pause_check.setToolTip(
            "Stops every running or waiting download immediately. "
            "New downloads stay paused until you turn this off."
        )
        self.pause_check.toggled.connect(self._on_toggle)
        cl.addWidget(self.pause_check)

        self.status_label = QLabel("")
        self.status_label.setObjectName("card_caption")
        self.status_label.setWordWrap(True)
        cl.addWidget(self.status_label)
        layout.addWidget(card)

        # ── card: auto-resume ──
        card, cl = self._card("Auto-Resume")
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Automatically resume after"))
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(0, 168)
        self.hours_spin.setSuffix(" h")
        self.hours_spin.setSpecialValueText("0 h (off)")
        self.hours_spin.setToolTip(
            "Hours until downloads resume automatically. "
            "0 keeps them paused until you flip the switch back."
        )
        self.hours_spin.valueChanged.connect(self._on_hours_changed)
        row.addWidget(self.hours_spin)
        row.addStretch(1)
        cl.addLayout(row)

        self.countdown_label = QLabel("")
        self.countdown_label.setObjectName("card_caption")
        cl.addWidget(self.countdown_label)
        layout.addWidget(card)

        # ── card: what it does ──
        card, cl = self._card("How it works")
        for line in (
            "• Waiting and running downloads pause right away.",
            "• Downloads added while paused stay in the paused list.",
            "• Flip the switch back on (or wait for the timer) to resume.",
        ):
            lbl = QLabel(line)
            lbl.setObjectName("card_caption")
            cl.addWidget(lbl)
        layout.addWidget(card)
        layout.addStretch(1)

        self.context.events.connect(
            Events.PAUSE_STATE_CHANGED, self._on_state_changed
        )
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_countdown)

        self.refresh()

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        lbl = QLabel(title)
        lbl.setObjectName("card_title")
        layout.addWidget(lbl)
        return frame, layout

    def _controller(self):
        return getattr(self.context, "pauses", None)

    # ── events ──────────────────────────────────────────────────────────────

    def _on_toggle(self, paused: bool) -> None:
        controller = self._controller()
        if controller is None:
            return
        if paused:
            controller.pause()
        else:
            controller.resume()

    def _on_hours_changed(self, hours: int) -> None:
        controller = self._controller()
        if controller is None:
            return
        controller.set_auto_resume_hours(hours)

    def _on_state_changed(self, payload) -> None:
        self.refresh()

    # ── rendering ───────────────────────────────────────────────────────────

    def refresh(self) -> None:
        controller = self._controller()
        paused = bool(controller.is_paused()) if controller is not None else False
        hours = (
            controller.auto_resume_hours()
            if controller is not None
            else 0
        )
        resume_at = (
            controller.resume_at() if controller is not None else None
        )

        self.pause_check.blockSignals(True)
        self.pause_check.setChecked(paused)
        self.pause_check.blockSignals(False)

        self.hours_spin.blockSignals(True)
        self.hours_spin.setValue(hours)
        self.hours_spin.blockSignals(False)

        if paused:
            if resume_at is not None:
                self.status_label.setText(
                    "Downloads are paused. They will resume automatically."
                )
                self._countdown_timer.start()
            else:
                self.status_label.setText(
                    "Downloads are paused. No auto-resume is set."
                )
                self._countdown_timer.stop()
        else:
            self.status_label.setText(
                "Downloads are running normally. Flip the switch to pause them."
            )
            self._countdown_timer.stop()
            self.countdown_label.setText("")
        self._update_countdown()

    def _update_countdown(self) -> None:
        controller = self._controller()
        if controller is None:
            return
        resume_at = controller.resume_at()
        if not controller.is_paused() or resume_at is None:
            self.countdown_label.setText("")
            return
        remaining = resume_at - datetime.now(resume_at.tzinfo)
        if remaining.total_seconds() <= 0:
            self.countdown_label.setText("Resuming…")
            return
        total_minutes = int(remaining.total_seconds()) // 60
        h, m = divmod(total_minutes, 60)
        self.countdown_label.setText(
            f"Resuming in {h} h {m:02d} min"
        )