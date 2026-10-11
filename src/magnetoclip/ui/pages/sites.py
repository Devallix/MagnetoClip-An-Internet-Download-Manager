"""Download Sites page: tabbed catalog of torrent, anime and streaming sites.

Each tab holds a table laid out like the Downloads page (site, description,
url, usage). The url cell is a link: clicking it hands the address to the
system browser.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.services.sites import SITE_CATEGORIES, SiteCategory

from ..components.color_header import ColorHeaderView
from .base import Page

_HEADERS = ("Site", "Description", "URL", "Usage")

_COL_SITE = 0
_COL_DESCRIPTION = 1
_COL_URL = 2
_COL_USAGE = 3

#: Per-theme palette for the table: header label, then the four cell colors.
_THEME_COLORS: dict[str, dict[str, tuple[str, str, str, str, str]]] = {
    "dark": {
        "header": ("#A78BFA", "#FBBF24", "#60A5FA", "#34D399"),
        "cells": ("#A78BFA", "#C3C9E0", "#60A5FA", "#34D399"),
    },
    "light": {
        "header": ("#7C3AED", "#B45309", "#2563EB", "#059669"),
        "cells": ("#6D28D9", "#39415A", "#1D4ED8", "#047857"),
    },
}


class DownloadSitesPage(Page):
    """Browse hand-picked torrent, anime and streaming sites."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        theme = context.settings.get("appearance.theme", "dark")
        self._colors = _THEME_COLORS.get(theme, _THEME_COLORS["dark"])
        self._tables: dict[str, QTableWidget] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(12)

        title = QLabel("Download Sites")
        title.setObjectName("page_title")
        outer.addWidget(title)

        subtitle = QLabel(
            "Hand-picked places to find media. Click a url to open it in your "
            "browser."
        )
        subtitle.setObjectName("page_subtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("sites_tabs")
        self.tabs.setDocumentMode(True)
        for category in SITE_CATEGORIES:
            self.tabs.addTab(self._build_tab(category), category.label)
        outer.addWidget(self.tabs, 1)

    # ----- construction -----

    def _build_tab(self, category: SiteCategory) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        table = self._build_table(category)
        self._tables[category.key] = table
        layout.addWidget(table, 1)
        return container

    def _build_table(self, category: SiteCategory) -> QTableWidget:
        table = QTableWidget(0, len(_HEADERS))
        table.setObjectName("sites_table")
        table.setHorizontalHeaderLabels(list(_HEADERS))
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(44)
        table.setWordWrap(True)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setFocusPolicy(Qt.NoFocus)
        # Descriptions wrap, so rows have to follow the text as the window
        # (and therefore the description column) is resized.
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.cellClicked.connect(self._on_cell_clicked)
        table.cellEntered.connect(self._on_cell_entered)

        header = ColorHeaderView(table)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(80)
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for column, color in enumerate(self._colors["header"]):
            header.set_section_color(column, color)
        header.set_section_alignment(_COL_USAGE, Qt.AlignCenter)
        table.setHorizontalHeader(header)

        table.setColumnWidth(_COL_SITE, 190)
        table.setColumnWidth(_COL_URL, 250)
        table.setColumnWidth(_COL_USAGE, 90)
        # The description is the only elastic column, so the table never
        # needs a horizontal scrollbar.
        header.setSectionResizeMode(_COL_DESCRIPTION, QHeaderView.Stretch)

        for row, entry in enumerate(category.entries):
            table.insertRow(row)
            self._fill_row(table, row, entry)
        return table

    def _fill_row(self, table: QTableWidget, row: int, entry) -> None:
        name, description, url, usage = (
            entry.name,
            entry.description,
            entry.url,
            entry.usage,
        )
        colors = self._colors["cells"]

        site_item = QTableWidgetItem(name)
        site_item.setForeground(QBrush(QColor(colors[0])))
        site_item.setToolTip(f"{name}\n{description}")
        site_item.setTextAlignment(Qt.AlignTop | Qt.AlignLeft)
        font = QFont(site_item.font())
        font.setBold(True)
        site_item.setFont(font)
        table.setItem(row, _COL_SITE, site_item)

        description_item = QTableWidgetItem(description)
        description_item.setForeground(QBrush(QColor(colors[1])))
        description_item.setToolTip(description)
        description_item.setTextAlignment(Qt.AlignTop | Qt.AlignLeft)
        table.setItem(row, _COL_DESCRIPTION, description_item)

        url_item = QTableWidgetItem(url)
        url_item.setForeground(QBrush(QColor(colors[2])))
        url_item.setToolTip(f"Open {url} in your browser")
        url_item.setTextAlignment(Qt.AlignTop | Qt.AlignLeft)
        table.setItem(row, _COL_URL, url_item)

        usage_item = QTableWidgetItem(usage)
        usage_item.setForeground(QBrush(QColor(colors[3])))
        usage_item.setTextAlignment(Qt.AlignTop | Qt.AlignHCenter)
        usage_item.setFont(font)
        table.setItem(row, _COL_USAGE, usage_item)

    # ----- interaction -----

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """Open the clicked url cell in the system browser."""
        if column != _COL_URL:
            return
        table = self.sender()
        item = table.item(row, _COL_URL) if table is not None else None
        if item is not None and item.text():
            QDesktopServices.openUrl(QUrl(item.text()))

    def _on_cell_entered(self, row: int, column: int) -> None:
        """Show a pointing-hand cursor over url cells only."""
        table = self.sender()
        if table is None:
            return
        table.viewport().setCursor(
            Qt.PointingHandCursor if column == _COL_URL else Qt.ArrowCursor
        )

    # ----- accessors -----

    def table_for(self, key: str) -> QTableWidget | None:
        return self._tables.get(key)

    def refresh(self) -> None:  # pragma: no cover - the catalog is static
        """The catalog never changes at runtime, so there is nothing to reload."""
