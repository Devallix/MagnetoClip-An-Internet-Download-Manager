"""About dialog: centered logo, description, and developer information."""

from __future__ import annotations

import platform
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from magnetoclip.resources import resource_path
from magnetoclip.version import __app_name__, __tagline__, __version__

_DESCRIPTION = (
    f"{__app_name__} is an advanced download manager that captures the web — "
    "files, videos, music, documents and more — into smart, auto-sorted "
    "categories."
)


def _dependency_versions() -> list[str]:
    versions = []
    for module, attr in (
        ("PySide6", "__version__"),
        ("PySide6.QtCore", "__version__"),
        ("sqlalchemy", "__version__"),
        ("httpx", "__version__"),
    ):
        try:
            imported = __import__(module, fromlist=[attr])
            versions.append(f"{module.split('.')[-1]} {getattr(imported, attr)}")
        except Exception:  # noqa: BLE001 - best-effort version collection
            versions.append(f"{module.split('.')[-1]} unknown")
    return versions


def _developer_image() -> Path:
    """Locate ``img/developer.png`` next to the source tree."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "img" / "developer.png"
        if candidate.exists():
            return candidate
    return here / "img" / "developer.png"


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {__app_name__}")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        content = QVBoxLayout()
        content.setSpacing(6)
        content.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)

        logo = QLabel()
        logo_path = resource_path("icons", "logo.png")
        pixmap = QPixmap(str(logo_path))
        if not pixmap.isNull():
            pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        logo.setPixmap(pixmap)
        logo.setFixedSize(80, 80)
        logo.setAlignment(Qt.AlignCenter)
        content.addWidget(logo)

        name = QLabel(__app_name__)
        name.setObjectName("about_name")
        name.setAlignment(Qt.AlignHCenter)
        content.addWidget(name)

        tagline = QLabel(__tagline__)
        tagline.setObjectName("about_tagline")
        tagline.setAlignment(Qt.AlignHCenter)
        content.addWidget(tagline)

        version_label = QLabel(f"Version {__version__}")
        version_label.setObjectName("about_version")
        version_label.setAlignment(Qt.AlignHCenter)
        content.addWidget(version_label)

        description = QLabel(_DESCRIPTION)
        description.setObjectName("about_description")
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignHCenter)
        content.addWidget(description)

        details = QLabel(
            "Python " + platform.python_version()
            + "\n" + "\n".join(_dependency_versions())
        )
        details.setObjectName("about_details")
        details.setAlignment(Qt.AlignHCenter)
        content.addWidget(details)

        developer = QLabel()
        dev_path = _developer_image()
        dev_pixmap = QPixmap(str(dev_path))
        if not dev_pixmap.isNull():
            dev_pixmap = dev_pixmap.scaled(
                180, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            developer.setPixmap(dev_pixmap)
        developer.setAlignment(Qt.AlignCenter)
        content.addWidget(developer)

        powered = QLabel("Powered by Devallix")
        powered.setObjectName("about_powered")
        powered.setAlignment(Qt.AlignHCenter)
        content.addWidget(powered)

        layout.addLayout(content, 1)

        separator = QLabel()
        separator.setObjectName("about_separator")
        separator.setFixedHeight(1)
        layout.addWidget(separator)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def show_about(parent=None) -> None:
    AboutDialog(parent=parent).exec()
