"""Read-only dialog that renders a bundled Markdown file."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
)

from magnetoclip.resources import resource_path


class MarkdownViewerDialog(QDialog):
    """Modal dialog that loads a Markdown file from ``resources/docs/`` and
    renders it as rich text in a read-only ``QTextBrowser``."""

    def __init__(self, filename: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(640, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setReadOnly(True)

        path = resource_path("docs", filename)
        if path.is_file():
            markdown = path.read_text(encoding="utf-8")
            browser.setMarkdown(markdown)
        else:
            browser.setPlainText(f"Document not found: {filename}")

        layout.addWidget(browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def show_user_guide(parent=None) -> None:
    MarkdownViewerDialog("USER_GUIDE.md", "MagnetoClip — User Guide", parent=parent).exec()


def show_eula(parent=None) -> None:
    MarkdownViewerDialog("EULA.md", "MagnetoClip — License Agreement", parent=parent).exec()
