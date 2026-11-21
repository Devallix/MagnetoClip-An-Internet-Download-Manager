"""DuplicateResultDialog: ask what to do when a download appears to be a duplicate."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..util import format_bytes, open_path


class DuplicateResultDialog(QDialog):
    """Present a detected duplicate and let the user choose.

    Returns one of ``"open"`` (open existing), ``"download"`` (proceed anyway),
    or ``"skip"`` (do not create a new download).
    """

    def __init__(self, payload: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Duplicate Detected")
        self.setMinimumWidth(460)
        self._result: str = "skip"

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("This file is already downloaded")
        title.setObjectName("page_title")
        layout.addWidget(title)

        hint = QLabel(
            "MagnetoClip found an existing file that matches this download."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)

        existing_path = payload.get("existing_path") or payload.get("save_path")
        existing_filename = (
            payload.get("existing_filename") or payload.get("filename") or "file"
        )
        existing_size = payload.get("existing_size") or 0
        categories = payload.get("existing_categories") or []

        info = QLabel(self._describe(existing_filename, existing_size, categories))
        info.setWordWrap(True)
        info.setObjectName("card_caption")
        layout.addWidget(info)

        if existing_path:
            path_label = QLabel(str(existing_path))
            path_label.setWordWrap(True)
            path_label.setTextInteractionFlags(
                path_label.textInteractionFlags() | path_label.textInteractionFlags().TextSelectableByMouse
            )
            path_label.setObjectName("muted")
            layout.addWidget(path_label)

        self._existing_path = existing_path
        self._save_path = payload.get("save_path")

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        open_btn = QPushButton("Open Existing")
        open_btn.clicked.connect(lambda: self._choose("open"))
        download_btn = QPushButton("Download Anyway")
        download_btn.clicked.connect(lambda: self._choose("download"))
        skip_btn = QPushButton("Skip")
        skip_btn.clicked.connect(lambda: self._choose("skip"))

        buttons.addWidget(open_btn)
        buttons.addWidget(download_btn)
        buttons.addStretch(1)
        buttons.addWidget(skip_btn)
        layout.addLayout(buttons)

    @staticmethod
    def _describe(filename: str, size: int, categories: list[str]) -> str:
        parts = [f"Name: {filename}"]
        if size:
            parts.append(f"Size: {format_bytes(size)}")
        if categories:
            parts.append("Categories: " + ", ".join(categories))
        return "  \u2022  ".join(parts)

    def _choose(self, result: str) -> None:
        if result == "open" and self._existing_path:
            open_path(self._existing_path)
        self._result = result
        self.accept()

    def result_action(self) -> str:
        return self._result
