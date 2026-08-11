"""Capture confirmation dialog shown when the browser hands off a file."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..components.buttons import AccentButton, GhostButton

RESULT_DOWNLOAD_NOW = 1
RESULT_DOWNLOAD_LATER = 2
RESULT_SKIP = 0
RESULT_SKIP_ALL = 3


class CaptureDialog(QDialog):
    """Presents the captured file's download information ready to download.

    The user can download immediately, add it to the queue, or skip it.
    """

    def __init__(
        self,
        context,
        *,
        url: str,
        filename: str | None = None,
        detected_type: str | None = None,
        referrer: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("Download with MagnetoClip")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Downloadable file detected")
        title.setObjectName("page_title")
        layout.addWidget(title)

        hint = QLabel("Confirm the details below and start downloading.")
        hint.setObjectName("page_subtitle")
        layout.addWidget(hint)

        info = QFrame()
        info.setObjectName("card")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(4)

        url_label = QLabel(url)
        url_label.setObjectName("card_caption")
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_label.setWordWrap(True)
        info_layout.addWidget(url_label)

        if detected_type:
            type_label = QLabel(f"Detected type: {detected_type}")
            type_label.setObjectName("card_caption")
            info_layout.addWidget(type_label)
        layout.addWidget(info)

        layout.addWidget(QLabel("Filename"))
        self.filename_edit = QLineEdit()
        self.filename_edit.setText(filename or "")
        self.filename_edit.setPlaceholderText("Keep the server-provided name")
        layout.addWidget(self.filename_edit)

        layout.addWidget(QLabel("Save to"))
        directory_row = QHBoxLayout()
        self.directory_edit = QLineEdit()
        self.directory_edit.setPlaceholderText("Default download folder")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        directory_row.addWidget(self.directory_edit, 1)
        directory_row.addWidget(self.browse_button)
        layout.addLayout(directory_row)

        options_row = QHBoxLayout()
        options_row.setSpacing(16)

        layout.addWidget(QLabel("Category"))
        self.category_combo = QComboBox()
        for category in getattr(getattr(context, "categories", None), "list", lambda: [])():
            self.category_combo.addItem(category.name)
        if self.category_combo.count() == 0:
            self.category_combo.addItem("Other")
        layout.addWidget(self.category_combo)

        connections_row = QHBoxLayout()
        connections_row.addWidget(QLabel("Connections"))
        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, 64)
        self.connections_spin.setValue(
            int(context.settings.get("downloads.connections_per_download", 8))
        )
        connections_row.addWidget(self.connections_spin)
        connections_row.addStretch(1)
        layout.addLayout(connections_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.skip_button = GhostButton("Skip")
        self.skip_button.clicked.connect(lambda: self.done(RESULT_SKIP))
        buttons.addWidget(self.skip_button)
        self.skip_all_button = GhostButton("Skip all")
        self.skip_all_button.clicked.connect(lambda: self.done(RESULT_SKIP_ALL))
        buttons.addWidget(self.skip_all_button)
        buttons.addStretch(1)
        self.later_button = GhostButton("Download later")
        self.later_button.clicked.connect(self._download_later)
        self.download_button = AccentButton("Download now")
        self.download_button.clicked.connect(self._download_now)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

        self.filename_edit.setFocus()

    def _download_now(self) -> None:
        if self._validate():
            self.done(RESULT_DOWNLOAD_NOW)

    def _download_later(self) -> None:
        if self._validate():
            self.done(RESULT_DOWNLOAD_LATER)

    def _validate(self) -> bool:
        if not self.filename_edit.text().strip():
            self.filename_edit.setFocus()
            return False
        return True

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose destination folder")
        if directory:
            self.directory_edit.setText(directory)

    def filename(self) -> str:
        return self.filename_edit.text().strip() or None

    def directory(self) -> str:
        return self.directory_edit.text().strip() or None

    def category(self) -> str | None:
        return self.category_combo.currentText()

    def connections(self) -> int | None:
        return self.connections_spin.value()
