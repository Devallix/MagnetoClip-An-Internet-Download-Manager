"""Detected page: downloadable files the extension found on visited pages."""

from __future__ import annotations

import base64
import datetime as _dt
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events
from magnetoclip.network.content import ensure_extension

from ..categories import (
    CATEGORY_LABELS,
    DETECTION_CATEGORIES,
    DETECTION_CATEGORY_LABELS,
    detection_category,
    normalize_detection_category,
)
from ..components.buttons import VerticalIconButton
from ..components.icons import tool_icon, type_icon
from ..util import format_bytes
from .base import Page

_HEADERS = ("", "File", "Size", "Type", "Page", "Detected")


def _type_label(detected_type: str | None) -> str:
    value = (detected_type or "other").lower()
    if value in CATEGORY_LABELS:
        return CATEGORY_LABELS[value]
    if value == "stream":
        return "Stream"
    return value.capitalize() or "Other"


def _format_detected(value) -> str:
    if not value:
        return "—"
    try:
        parsed = _dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def _page_label(page_url: str) -> str:
    """Site name for the Page column; the full URL stays in the tooltip."""
    if not page_url:
        return "—"
    try:
        host = urlparse(page_url).hostname
    except ValueError:
        return "—"
    if not host:
        return "—"
    return host.removeprefix("www.") or "—"


class DetectedPage(Page):
    """Table of files found on browsed pages, with download/remove actions."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self._rows: dict[str, int] = {}
        self._detection_ids: list[int] = []
        self._files: list[dict] = []
        self._all_files: list[dict] = []
        self._empty_row: int | None = None
        self._category = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(12)

        outer.addLayout(self._build_toolbar())

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setObjectName("detected_table")
        self.table.setHorizontalHeaderLabels(list(_HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(60)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(4, 200)
        self.table.setColumnWidth(5, 150)

        outer.addWidget(self.table, 1)

        context.events.connect(Events.BROWSER_EVENT, self._on_browser_event)

        self.refresh()

    # ----- construction -----

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.select_all_button = VerticalIconButton(tool_icon("select_all"), "Select All")
        self.select_all_button.setObjectName("tool_button")
        self.select_all_button.setProperty("tint", "select_all")
        self.select_all_button.clicked.connect(self._toggle_select_all)
        self.download_button = VerticalIconButton(tool_icon("add"), "Download")
        self.download_button.setObjectName("tool_button")
        self.download_button.setProperty("tint", "add")
        self.download_button.clicked.connect(self._download_selected)
        self.remove_button = VerticalIconButton(tool_icon("remove"), "Remove")
        self.remove_button.setObjectName("tool_button")
        self.remove_button.setProperty("role", "danger")
        self.remove_button.clicked.connect(self._remove_selected)

        row.addWidget(self.select_all_button)
        row.addWidget(self.download_button)
        row.addWidget(self.remove_button)

        row.addSpacing(12)
        filters = QHBoxLayout()
        filters.setSpacing(6)
        self._filter_buttons: dict[str, QPushButton] = {}
        for key, label in [("all", "All"), *[(k, DETECTION_CATEGORY_LABELS[k]) for k in DETECTION_CATEGORIES]]:
            button = QPushButton(label)
            button.setObjectName("detected_filter")
            button.setCheckable(True)
            button.setProperty("category", key)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, k=key: self.set_filter(k))
            filters.addWidget(button)
            self._filter_buttons[key] = button
        row.addLayout(filters)
        row.addStretch(1)
        self._set_filter_button("all")
        return row

    # ----- category filter -----

    def set_filter(self, category: str | None) -> None:
        """Show every category, or only one of the extension's six."""
        self._category = normalize_detection_category(category)
        self._set_filter_button(self._category or "all")
        self.refresh()

    def _set_filter_button(self, key: str) -> None:
        for button_key, button in self._filter_buttons.items():
            button.setChecked(button_key == key)

    # ----- selection -----

    def _selected_indexes(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                selected.append(row)
        return selected

    def _all_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount()) if self.table.item(r, 0) is not None]

    def _toggle_select_all(self) -> None:
        rows = self._all_rows()
        if not rows:
            return
        all_checked = all(self.table.item(r, 0).checkState() == Qt.Checked for r in rows)
        state = Qt.Unchecked if all_checked else Qt.Checked
        self.table.blockSignals(True)
        try:
            for r in rows:
                self.table.item(r, 0).setCheckState(state)
        finally:
            self.table.blockSignals(False)
        self._update_action_states()

    def _update_action_states(self) -> None:
        has_selection = bool(self._selected_indexes())
        self.download_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)
        self.select_all_button.setEnabled(bool(self._all_rows()))

    # ----- actions -----

    def start_entry_download(self, entry: dict[str, Any]) -> int | None:
        """Start one detected entry using the normal Detected-page pipeline.

        This is intentionally the same manager.add()/start()/remove sequence
        used by the visible Download button. It gives player-originated HLS
        downloads the same request context, save-path selection, and lifecycle
        as a user downloading the corresponding row from Detected.
        """
        cookies = self._entry_cookies(entry)
        from ..youtube_flow import youtube_quality_kwargs

        quality_kwargs = youtube_quality_kwargs(
            self.context,
            entry["url"],
            cookies=cookies or None,
            parent=self,
        )
        if quality_kwargs is None:
            return None

        data = self._decode_data(entry.get("data_base64"))
        download = self.context.manager.add(
            entry["url"],
            filename=ensure_extension(
                entry.get("filename") or None, None, data
            ),
            headers=self._entry_headers(entry),
            cookies=cookies,
            data=data,
            request_context=self._entry_request_context(entry, cookies),
            **quality_kwargs,
        )
        self._start(download.id)
        self._remove_entry(entry)
        return download.id

    def _download_selected(self) -> None:
        rows = self._selected_indexes()
        failures: list[str] = []
        added = 0

        for row in rows:
            entry = self._files[row]
            try:
                download_id = self.start_entry_download(entry)
            except Exception as exc:  # noqa: BLE001 - report bad entries, keep going
                failures.append(f"{entry['url']}: {exc}")
                continue
            if download_id is not None:
                added += 1
        if failures:
            first = failures[0]
            if len(first) > 160:
                first = first[:157] + "..."
            suffix = f" (and {len(failures) - 1} more)" if len(failures) > 1 else ""
            self.context.events.post(
                Events.NOTIFICATION_REQUESTED,
                {
                    "kind": "error",
                    "title": "Could not add some downloads",
                    "body": first + suffix,
                },
            )
        if added or failures:
            self.refresh()

    @staticmethod
    def _decode_data(data_base64: str | None) -> bytes | None:
        if not data_base64:
            return None
        try:
            return base64.b64decode(data_base64, validate=True)
        except Exception:  # noqa: BLE001 - corrupt data falls back to a normal add
            return None

    @staticmethod
    def _entry_cookies(entry: dict[str, Any]) -> dict[str, str]:
        """Return a detection's cookies as a mapping.

        Older rows (and the ``pending_captures`` bridge) hold the raw
        ``"a=1; b=2"`` header string. The download manager merges cookies by
        name, so handing it a string raises and the download silently fails.
        """
        cookies = entry.get("cookies")
        if not cookies:
            return {}
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if v is not None}
        from magnetoclip.network.cookies.jar import parse_cookie_header

        return parse_cookie_header(str(cookies)) or {}

    def _entry_headers(self, entry: dict[str, Any]) -> dict[str, str] | None:
        """Captured request headers, with the page URL as a Referer fallback."""
        headers: dict[str, str] = {}
        for key, value in (entry.get("headers") or {}).items():
            if value is not None and str(key).strip():
                headers[str(key)] = str(value)
        if not any(k.lower() == "referer" for k in headers):
            referer = entry.get("frame_url") or entry.get("page_url")
            if referer:
                headers["Referer"] = str(referer)
        return headers or None

    def _entry_request_context(
        self, entry: dict[str, Any], cookies: dict[str, str]
    ):
        """Rebuild the ``RequestContext`` a stream download needs.

        Manifest URLs (m3u8/mpd) are not downloadable on their own: the
        segment and key requests have to replay the cookies, user agent and
        player frame that the browser used, otherwise a bot-walled CDN answers
        403 and the download dies after the manifest is saved. This mirrors
        what the extension used to send inline for immediate captures.
        """
        from magnetoclip.media.models import RequestContext

        headers = self._entry_headers(entry) or {}
        frame_url = entry.get("frame_url") or entry.get("referrer") or None
        return RequestContext.from_headers(
            headers=headers,
            cookies=cookies,
            user_agent=entry.get("user_agent") or None,
            page_url=entry.get("page_url") or None,
            frame_url=frame_url,
            source=entry.get("source") or "detected_page",
        )

    def _start(self, download_id: int) -> None:
        import asyncio

        manager = self.context.manager
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._start_async(manager, download_id))

    @staticmethod
    async def _start_async(manager, download_id: int) -> None:
        manager.start(download_id)

    def _remove_selected(self) -> None:
        entries = [self._files[row] for row in self._selected_indexes()]
        if not entries:
            return
        self._acknowledge_removed(entries)
        with self.context.session_factory() as session:
            from magnetoclip.database.repositories import BrowserDetectionRepository

            BrowserDetectionRepository(session).remove_urls_everywhere(
                {entry["url"] for entry in entries}
            )
        self.refresh()

    def _remove_entry(self, entry: dict) -> None:
        # A started download leaves the queue on both sides: the app removes its
        # Detected row and the extension must forget the same URLs.
        self._acknowledge_removed([entry])
        with self.context.session_factory() as session:
            from magnetoclip.database.repositories import BrowserDetectionRepository

            BrowserDetectionRepository(session).remove_file_everywhere(entry["url"])

    def _acknowledge_removed(self, entries: list[dict]) -> None:
        """Tell the extension its queue can drop these entries.

        Without this the extension would keep re-syncing, and its badge would
        keep counting files the user already dealt with.
        """
        urls = [entry["url"] for entry in entries if entry.get("url")]
        ids = [entry.get("extension_id") for entry in entries if entry.get("extension_id")]
        if not urls and not ids:
            return
        try:
            with self.context.session_factory() as session:
                from magnetoclip.database.repositories import BrowserRequestRepository

                BrowserRequestRepository(session).add(
                    "detections_handled",
                    payload={"urls": urls, "ids": ids},
                )
        except Exception:  # noqa: BLE001 - a stale queue entry is not worth blocking on
            return

    # ----- events -----

    def _on_browser_event(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("source") in (
            "page_scan",
            "detection_sync",
        ):
            self.refresh()

    # ----- row management -----

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_action_states()

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column == 0:
            return
        item = self.table.item(row, 0)
        if item is not None and item.checkState() != Qt.Checked:
            item.setCheckState(Qt.Checked)
        self.table.selectRow(row)

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        if row < 0 or row >= len(self._files):
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        self.table.blockSignals(True)
        try:
            item.setCheckState(Qt.Checked)
        finally:
            self.table.blockSignals(False)
        self._download_selected()

    def _show_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row >= 0:
            self._on_cell_clicked(row, 1)
        menu = QMenu(self)
        download_action = menu.addAction("Download")
        copy_action = menu.addAction("Copy URL")
        menu.addSeparator()
        remove_action = menu.addAction("Remove Selected")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is download_action:
            self._download_selected()
        elif chosen is copy_action:
            urls = [self._files[r]["url"] for r in self._selected_indexes()]
            if urls:
                from PySide6.QtWidgets import QApplication

                QApplication.clipboard().setText("\n".join(urls))
        elif chosen is remove_action:
            self._remove_selected()

    def _clear_empty_row(self) -> None:
        if self._empty_row is None:
            return
        self.table.removeRow(self._empty_row)
        self._empty_row = None

    def _update_empty_state(self) -> None:
        if self.table.rowCount() == 0:
            self.table.insertRow(0)
            self._empty_row = 0
            self.table.setSpan(0, 1, 1, len(_HEADERS) - 1)
            item = QTableWidgetItem("No detected files yet. Browse the web with the extension enabled.")
            item.setFlags(Qt.NoItemFlags)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(0, 1, item)

    def refresh(self) -> None:
        with self.context.session_factory() as session:
            from magnetoclip.database.repositories import BrowserDetectionRepository

            detections = BrowserDetectionRepository(session).list_detections(limit=500)

        seen: set[str] = set()
        entries: list[dict] = []
        for detection in detections:
            files = detection.files_json or []
            if not files:
                files = [{"url": detection.page_url}]
            for file in files:
                url = str(file.get("url") or "").strip()
                if not url:
                    continue
                if url in seen:
                    continue
                # Blob/data URIs without inline bytes can never be downloaded
                # from here (only the page that created them can resolve them);
                # showing them just produces silent failures.
                if url.startswith(("blob:", "data:")) and not file.get("data_base64"):
                    continue
                detected_type = file.get("detected_type") or "other"
                category = detection_category(detected_type, file.get("category"))
                seen.add(url)
                entries.append(
                    {
                        "detection_id": detection.id,
                        "url": url,
                        "filename": file.get("filename") or "",
                        "detected_type": detected_type,
                        "category": category,
                        "page_url": detection.page_url,
                        "created_at": detection.created_at,
                        "cookies": file.get("cookies") or None,
                        "data_base64": file.get("data_base64"),
                        "extension_id": file.get("extension_id") or "",
                        # Capture context, needed verbatim to download a
                        # manifest or a session-gated file. Omitting any of
                        # these here silently strips them from the request.
                        "headers": file.get("headers") or None,
                        "user_agent": file.get("user_agent") or None,
                        "referrer": file.get("referrer") or None,
                        "frame_url": file.get("frame_url") or None,
                        "source": file.get("source") or None,
                    }
                )

        # The extension owns the six-category vocabulary, so the filter is applied
        # to the resolved category rather than to the raw detected type.
        self._all_files = entries
        visible = [e for e in entries if not self._category or e["category"] == self._category]

        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._rows.clear()
        self._detection_ids.clear()
        self._files = visible
        self._empty_row = None
        for entry in visible:
            self._insert_entry(entry)
        self._update_empty_state()
        self.table.blockSignals(False)
        self._update_action_states()

    def _update_empty_state(self) -> None:
        if self.table.rowCount() != 0:
            return
        self.table.insertRow(0)
        self._empty_row = 0
        self.table.setSpan(0, 1, 1, len(_HEADERS) - 1)
        label = DETECTION_CATEGORY_LABELS.get(self._category, "")
        text = (
            f"No detected {label.lower()} yet. Browse the web with the extension enabled."
            if label
            else "No detected files yet. Browse the web with the extension enabled."
        )
        item = QTableWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(0, 1, item)

    def _insert_entry(self, entry: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        check.setCheckState(Qt.Unchecked)
        self.table.setItem(row, 0, check)

        name = entry.get("filename") or entry["url"].rsplit("/", 1)[-1] or entry["url"]
        name_item = QTableWidgetItem(name)
        name_item.setIcon(type_icon(entry.get("detected_type")))
        name_item.setToolTip(entry["url"])
        self.table.setItem(row, 1, name_item)

        size = entry.get("size")
        self.table.setItem(row, 2, QTableWidgetItem(format_bytes(size) if size else "—"))

        self.table.setItem(row, 3, QTableWidgetItem(_type_label(entry.get("detected_type"))))
        page_url = entry.get("page_url") or ""
        page_item = QTableWidgetItem(_page_label(page_url))
        page_item.setToolTip(page_url or "")
        self.table.setItem(row, 4, page_item)
        self.table.setItem(row, 5, QTableWidgetItem(_format_detected(entry.get("created_at"))))
