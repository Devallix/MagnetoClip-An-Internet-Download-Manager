"""Speed test page: run a network speed test and view history."""

from __future__ import annotations

import math
import threading
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.core.events.bus import Events

from ..components.buttons import AccentButton, GhostButton
from ..components.stat_card import StatCard
from .base import Page, make_scrollable

# ── Palette tokens (dark theme) ─────────────────────────────────────────────
_SURFACE = "#131624"
_SURFACE_ALT = "#1A1E31"
_BORDER = "#262B44"
_TEXT = "#E6E9F5"
_TEXT_MUTED = "#8A92B5"
_ACCENT = "#8B5CF6"
_ACCENT_BLUE = "#3B82F6"
_ACCENT_CYAN = "#22D3EE"
_SUCCESS = "#34D399"
_WARNING = "#FBBF24"
_DANGER = "#F87171"


def _speed_color(mbps: float) -> QColor:
    """Return a colour that reflects connection quality."""
    if mbps >= 50:
        return QColor(_SUCCESS)
    if mbps >= 20:
        return QColor(_ACCENT_CYAN)
    if mbps >= 5:
        return QColor(_WARNING)
    return QColor(_DANGER)


class _CircularGauge(QWidget):
    """Animated circular gauge that shows download / upload speed.

    The gauge draws two concentric arcs (download outer, upload inner) with a
    glowing tip, centred speed text, and a pulsing ring while a test runs.
    """

    _MAX_SCALE = 100.0  # Mbps – arcs are normalised against this

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._dl = 0.0
        self._ul = 0.0
        self._dl_target = 0.0
        self._ul_target = 0.0
        self._pulse = 0.0
        self._running = False
        self._center: float | None = None

        # Smooth animation for the speed value
        self._anim = QPropertyAnimation(self, b"dlValue")
        self._anim.setDuration(600)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._anim_ul = QPropertyAnimation(self, b"ulValue")
        self._anim_ul.setDuration(600)
        self._anim_ul.setEasingCurve(QEasingCurve.OutCubic)

    # -- Qt properties for animation ------------------------------------------

    @Property(float)
    def dlValue(self) -> float:
        return self._dl

    @dlValue.setter
    def dlValue(self, v: float) -> None:
        self._dl = v
        self.update()

    @Property(float)
    def ulValue(self) -> float:
        return self._ul

    @ulValue.setter
    def ulValue(self, v: float) -> None:
        self._ul = v
        self.update()

    # -- public API -----------------------------------------------------------

    def set_speeds(self, dl_mbps: float, ul_mbps: float) -> None:
        self._dl_target = dl_mbps
        self._ul_target = ul_mbps
        self._anim.stop()
        self._anim.setStartValue(self._dl)
        self._anim.setEndValue(dl_mbps)
        self._anim.start()
        self._anim_ul.stop()
        self._anim_ul.setStartValue(self._ul)
        self._anim_ul.setEndValue(ul_mbps)
        self._anim_ul.start()

    def set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self._pulse = 0.0
            self._tick_pulse()
        self.update()

    def set_center_value(self, value: float | None) -> None:
        """Show *value* in the centre of the gauge (e.g. upload speed during
        an upload-only test); ``None`` restores the download-speed display."""
        if value is not None:
            value = max(0.0, value)
        self._center = value
        self.update()

    def _tick_pulse(self) -> None:
        if not self._running:
            return
        self._pulse = (self._pulse + 0.06) % (2 * math.pi)
        self.update()
        QTimer.singleShot(30, self._tick_pulse)

    # -- painting -------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 16
        if radius < 40:
            p.end()
            return

        # Spans: 225° to -45°  (270° sweep, gap at bottom-right)
        start_deg = 225
        sweep_deg = 270
        max_span = int(sweep_deg * 16)

        def _draw_arc(r: float, ratio: float, color: QColor, pen_w: float) -> None:
            rect_size = r * 2
            rect = self.rect()
            rect.setWidth(int(rect_size))
            rect.setHeight(int(rect_size))
            rect.moveCenter(self.rect().center())
            # background track
            bg = QColor(_BORDER)
            p.setPen(QPen(bg, pen_w, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, int(start_deg * 16), max_span)
            # value arc
            if ratio > 0.005:
                p.setPen(QPen(color, pen_w, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(rect, int(start_deg * 16), int(sweep_deg * ratio * 16))

        # ── outer arc: download ──
        dl_ratio = min(1.0, self._dl / self._MAX_SCALE)
        dl_color = _speed_color(self._dl)
        _draw_arc(radius, dl_ratio, dl_color, 10)

        # ── inner arc: upload ──
        ul_ratio = min(1.0, self._ul / self._MAX_SCALE)
        ul_color = QColor(_ACCENT_BLUE)
        _draw_arc(radius - 18, ul_ratio, ul_color, 6)

        # ── pulsing ring while running ──
        if self._running:
            alpha = int(80 + 80 * math.sin(self._pulse))
            pulse_color = QColor(_ACCENT)
            pulse_color.setAlpha(alpha)
            p.setPen(QPen(pulse_color, 2, Qt.SolidLine, Qt.RoundCap))
            pulse_r = radius + 6 + 4 * math.sin(self._pulse)
            pulse_rect = self.rect()
            pulse_rect.setWidth(int(pulse_r * 2))
            pulse_rect.setHeight(int(pulse_r * 2))
            pulse_rect.moveCenter(self.rect().center())
            p.drawEllipse(pulse_rect)

        # ── centre text ──
        p.setPen(QColor(_TEXT))
        big = QFont()
        big.setPixelSize(max(28, int(radius * 0.34)))
        big.setBold(True)
        p.setFont(big)
        center_value = self._dl if self._center is None else self._center
        if center_value > 0 or self._running:
            text = f"{center_value:.1f}"
        else:
            text = "--"
        p.drawText(self.rect(), Qt.AlignCenter, text)

        # "Mbps" label below the number
        p.setPen(QColor(_TEXT_MUTED))
        small = QFont()
        small.setPixelSize(max(12, int(radius * 0.13)))
        p.setFont(small)
        label_rect = self.rect()
        label_rect.moveCenter(self.rect().center())
        label_rect.translate(0, max(18, int(radius * 0.24)))
        p.drawText(label_rect, Qt.AlignCenter, "Mbps")

        # ── legend dots ──
        dot_y = cy + radius - 10
        dot_size = 8
        for i, (clr, _lbl) in enumerate(
            [(dl_color, "↓"), (ul_color, "↑")]
        ):
            dx = cx - 30 + i * 60
            p.setBrush(QBrush(clr))
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(dx - dot_size / 2), int(dot_y - dot_size / 2), dot_size, dot_size)

        p.end()


class SpeedTestPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self._busy = False
        self._upload_phase = False
        self._live_dl = 0.0
        self._live_ul = 0.0
        self._live_latency: float | None = None

        layout = make_scrollable(self)

        # ── header ──
        header = QVBoxLayout()
        title = QLabel("Network Speed Test")
        title.setObjectName("page_title")
        subtitle = QLabel("Measure throughput, latency, and ISP throttling")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        # ── controls ──
        controls = QHBoxLayout()
        self.run_button = AccentButton("Run Test")
        self.run_button.clicked.connect(self._run_test)
        controls.addWidget(self.run_button)
        self.upload_button = GhostButton("Upload Test")
        self.upload_button.clicked.connect(lambda: self._run_test(upload=True))
        controls.addWidget(self.upload_button)
        self.export_button = GhostButton("Export CSV")
        self.export_button.clicked.connect(self._export_csv)
        controls.addWidget(self.export_button)
        controls.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("card_caption")
        controls.addWidget(self.status_label)
        layout.addLayout(controls)

        # ── circular gauge ──
        self.gauge = _CircularGauge()
        gauge_row = QHBoxLayout()
        gauge_row.addStretch(1)
        gauge_row.addWidget(self.gauge)
        gauge_row.addStretch(1)
        layout.addLayout(gauge_row)

        # ── stat cards ──
        cards = QHBoxLayout()
        self.download_card = StatCard("Download")
        self.download_card.set_accent(_SUCCESS)
        self.upload_card = StatCard("Upload")
        self.upload_card.set_accent(_ACCENT_BLUE)
        self.latency_card = StatCard("Latency")
        self.latency_card.set_accent(_ACCENT)
        self.server_card = StatCard("Server")
        self.server_card.set_accent(_ACCENT_CYAN)
        cards.addWidget(self.download_card)
        cards.addWidget(self.upload_card)
        cards.addWidget(self.latency_card)
        cards.addWidget(self.server_card)
        layout.addLayout(cards)

        # ── ISP info ──
        self.isp_label = QLabel("")
        self.isp_label.setObjectName("card_caption")
        layout.addWidget(self.isp_label)

        # ── history ──
        history_title = QLabel("History")
        history_title.setObjectName("card_title")
        layout.addWidget(history_title)
        self.history_label = QLabel("")
        self.history_label.setObjectName("card_caption")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)

        # ── throttle ──
        throttle_title = QLabel("ISP Throttling")
        throttle_title.setObjectName("card_title")
        layout.addWidget(throttle_title)
        self.throttle_label = QLabel("")
        self.throttle_label.setObjectName("card_caption")
        self.throttle_label.setWordWrap(True)
        layout.addWidget(self.throttle_label)

        self.context.events.connect(Events.SPEED_TEST_COMPLETED, self._on_completed)
        self.context.events.connect(Events.SPEED_TEST_PROGRESS, self._on_progress)
        self.refresh()

    # ── data ─────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        service = getattr(self.context, "speedtest", None)
        if service is None:
            return
        history = service.history(limit=12)
        throttle = service.throttle_analysis()

        latest = history[0] if history else {}
        self.download_card.set_value(
            f"{latest['download_mbps']:.1f} Mbps"
            if latest.get("download_mbps") is not None
            else "--"
        )
        self.upload_card.set_value(
            f"{latest['upload_mbps']:.1f} Mbps"
            if latest.get("upload_mbps") is not None
            else "--"
        )
        self.latency_card.set_value(
            f"{latest['latency_ms']:.0f} ms"
            if latest.get("latency_ms") is not None
            else "--"
        )
        self.server_card.set_value(latest.get("server_name") or "--")
        if history:
            dl = latest.get("download_mbps") or 0
            ul = latest.get("upload_mbps") or 0
            self._live_dl = dl
            self._live_ul = ul
            self._upload_phase = False
            self.gauge.set_speeds(dl, ul)
            self.gauge.set_center_value(None)
            isp = latest.get("isp_name")
            self.isp_label.setText(f"ISP: {isp}" if isp else "")
        else:
            self._live_dl = 0.0
            self._live_ul = 0.0
            self._upload_phase = False
            self.gauge.set_center_value(None)
            self.isp_label.setText("")

        if history:
            lines = []
            for entry in history[:8]:
                lines.append(
                    f"{entry.get('ts')}: {entry.get('download_mbps') or 0:.1f} "
                    f"Mbps down / {entry.get('upload_mbps') or 0:.1f} up / "
                    f"{entry.get('latency_ms') or 0:.0f} ms"
                )
            self.history_label.setText("\n".join(lines))
        else:
            self.history_label.setText("Run a test to populate history.")

        if throttle.get("flagged"):
            self.throttle_label.setText(
                f"{throttle.get('reason')} — possible throttling detected."
            )
        else:
            self.throttle_label.setText(
                throttle.get("reason")
                or "No throttling detected yet. Run tests over time to build a baseline."
            )

    # ── test execution ───────────────────────────────────────────────────────

    def _run_test(self, upload: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        self._upload_phase = upload
        self._live_dl = 0.0
        self._live_ul = 0.0
        self._live_latency = None
        self.run_button.setEnabled(False)
        self.upload_button.setEnabled(False)
        self.status_label.setText("Starting…")
        self.gauge.set_running(True)
        self.gauge.set_speeds(0.0, 0.0)
        self.gauge.set_center_value(None)
        self.download_card.set_value("--")
        self.upload_card.set_value("--")
        self.latency_card.set_value("--")
        service = getattr(self.context, "speedtest", None)
        if service is None:
            self.status_label.setText("Speed test unavailable")
            self._busy = False
            self.run_button.setEnabled(True)
            self.upload_button.setEnabled(True)
            self.gauge.set_running(False)
            return
        threading.Thread(
            target=self._run_sync, args=(service, upload), daemon=True
        ).start()

    def _run_sync(self, service, upload: bool) -> None:
        try:
            service.run_test_blocking(upload=upload)
        except Exception as exc:  # noqa: BLE001
            # run_test_blocking rarely raises (measure() swallows errors), but
            # on an unexpected failure make sure the UI is released and the
            # error surfaces through the normal completion path.
            self.context.events.post(
                Events.SPEED_TEST_COMPLETED, {"error": str(exc)}
            )

    def _on_completed(self, payload) -> None:
        self._busy = False
        self._upload_phase = False
        self.run_button.setEnabled(True)
        self.upload_button.setEnabled(True)
        self.gauge.set_running(False)
        self.gauge.set_center_value(None)
        if payload.get("error"):
            self.status_label.setText(f"Error: {payload['error'][:60]}")
        else:
            self.status_label.setText("Done")
        self.refresh()

    def _on_progress(self, payload) -> None:
        progress = payload.get("progress")
        phase = payload.get("phase")
        if phase == "upload":
            self._upload_phase = True
        elif phase == "download":
            self._upload_phase = False
        if progress is not None:
            label = "Upload" if self._upload_phase else "Download"
            self.status_label.setText(f"{label}… {int(progress * 100)}%")
        if payload.get("download_mbps") is not None:
            dl = float(payload["download_mbps"])
            self._live_dl = dl
            self.download_card.set_value(f"{dl:.1f} Mbps")
        if payload.get("upload_mbps") is not None:
            ul = float(payload["upload_mbps"])
            self._live_ul = ul
            if self._upload_phase:
                self.gauge.set_center_value(ul)
            self.upload_card.set_value(f"{ul:.1f} Mbps")
        if payload.get("latency_ms") is not None:
            ms = float(payload["latency_ms"])
            self._live_latency = ms
            self.latency_card.set_value(f"{ms:.0f} ms")
        if self._upload_phase:
            self.gauge.set_speeds(0.0, self._live_ul)
        else:
            self.gauge.set_speeds(self._live_dl, self._live_ul)

    # ── export ───────────────────────────────────────────────────────────────

    def _export_csv(self) -> None:
        service = getattr(self.context, "speedtest", None)
        if service is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Speed Test History", "speed_tests.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            Path(path).write_text(service.csv_export(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))
