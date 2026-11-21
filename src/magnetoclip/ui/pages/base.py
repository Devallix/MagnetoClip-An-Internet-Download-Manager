"""Base class for application pages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget


class Page(QFrame):
    """A page hosted in the main window's stacked widget."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("page")

    def refresh(self) -> None:  # pragma: no cover - optional hook
        """Reload data shown by this page."""


def make_scrollable(
    page: Page,
    margins: tuple[int, int, int, int] = (24, 20, 24, 20),
    spacing: int = 16,
    *,
    horizontal: bool = False,
    min_width: int | None = None,
) -> QVBoxLayout:
    """Wrap *page* in a scroll area and return the layout for its content.

    The content layout is placed inside a scrollable container so content
    scrolls instead of being squished when the window is too small. Pages
    with wide content can opt into horizontal scrolling via *horizontal*;
    *min_width* keeps the container wider than the viewport so there is
    actually something to scroll across.
    """
    scroll = QScrollArea(page)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(
        Qt.ScrollBarAsNeeded if horizontal else Qt.ScrollBarAlwaysOff
    )

    container = QWidget()
    container.setObjectName(page.objectName())
    if min_width is not None:
        container.setMinimumWidth(min_width)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    scroll.setWidget(container)

    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(scroll)
    return layout
