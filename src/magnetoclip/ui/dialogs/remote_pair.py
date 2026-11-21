"""Remote pairing dialog: QR code + pairing URL for the LAN dashboard."""

from __future__ import annotations

import io

import segno
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


def _qr_pixmap(content: str, size: int = 220) -> QPixmap:
    """Render *content* as a QR code pixmap using segno (no extra deps)."""
    qr = segno.make(content, error="h")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=8, border=2, dark="#0B0D14", light="#FFFFFF")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    if not pixmap.isNull():
        pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pixmap


class RemotePairDialog(QDialog):
    """Shows the scannable pairing URL for the remote dashboard."""

    def __init__(self, server, parent=None) -> None:
        super().__init__(parent)
        self._server = server
        self.setWindowTitle("Pair Remote Control")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        url = server.pair_url()
        hint = QLabel(
            "Scan with your phone to open the MagnetoClip dashboard.\n"
            "Works on your local network only."
        )
        hint.setObjectName("about_description")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignHCenter)
        layout.addWidget(hint)

        qr = QLabel()
        qr.setAlignment(Qt.AlignCenter)
        pixmap = _qr_pixmap(url)
        if not pixmap.isNull():
            qr.setPixmap(pixmap)
        else:
            qr.setText("(QR unavailable)")
        layout.addWidget(qr)

        url_edit = QLineEdit(url)
        url_edit.setReadOnly(True)
        layout.addWidget(url_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def show_pair_dialog(server, parent=None) -> None:
    if not getattr(server, "running", False):
        return
    RemotePairDialog(server, parent=parent).exec()
