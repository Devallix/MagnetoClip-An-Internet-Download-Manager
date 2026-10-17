"""Read-only dialog that renders a bundled Markdown file."""

from __future__ import annotations

import re
from collections.abc import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QTextBlock, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
)

from magnetoclip.resources import resource_path

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_MARKUP_RE = re.compile(r"[`*_~]")


def _display_text(title: str) -> str:
    """Strip inline emphasis/backticks so a heading matches its plain text."""
    return _MARKUP_RE.sub("", title).strip()


def _slugify(title: str) -> str:
    """GitHub-style anchor slug: lowercase alphanumerics separated by hyphens."""
    text = _display_text(title).lower()
    return re.sub(r"[^0-9a-z]+", "-", text).strip("-")


def _collect_headings(markdown: str) -> list[tuple[int, str, str]]:
    """Return ``(level, slug, display)`` for every heading, skipping any that
    sit inside fenced code blocks."""
    headings: list[tuple[int, str, str]] = []
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(stripped)
        if match:
            level = len(match.group(1))
            display = _display_text(match.group(2))
            slug = _slugify(match.group(2))
            if display and slug:
                headings.append((level, slug, display))
    return headings


def _build_toc(headings: list[tuple[int, str, str]]) -> str:
    """A markdown table of contents linking to the documented headings."""
    lines = ["", "## Table of contents", ""]
    for level, slug, display in headings:
        if level <= 1:
            continue
        indent = "" if level <= 2 else "    "
        lines.append(f"{indent}- [{display}](#{slug})")
    return "\n".join(lines)


def _preprocess(
    markdown: str,
) -> tuple[str, dict[str, str]]:
    """Add a table of contents when the document has none, and return the
    slug → heading-text map used to jump to a section on click.

    Qt's markdown renderer does not create named anchors for headings, so the
    links rendered from ``[text](#slug)`` have no target and do nothing on
    click. The viewer intercepts the click in its browser widget and scrolls
    to the heading (see ``_TocTextBrowser`` and ``_jump_to_heading``).
    """
    headings = _collect_headings(markdown)
    anchor_map: dict[str, str] = {}
    for _level, slug, display in headings:
        anchor_map.setdefault(slug, display)

    has_toc = any(display.lower() == "table of contents" for _, _, display in headings)
    if has_toc:
        return markdown, anchor_map

    block = _build_toc(headings)
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 1:
            lines.insert(index + 1, block)
            return "\n".join(lines), anchor_map
    return f"{block}\n\n{markdown}", anchor_map


def _block_has_hyperlink(block: QTextBlock) -> bool:
    """True when the block renders any ``[text](#slug)`` hyperlink fragment."""
    it = block.begin()
    while not it.atEnd():
        fmt = it.fragment().charFormat()
        if fmt.isAnchor() and fmt.anchorHref():
            return True
        it += 1
    return False


def _jump_to_heading(
    browser: QTextBrowser,
    anchor_map: dict[str, str],
    fragment: str,
) -> bool:
    """Scroll *browser* to the heading behind *fragment*. Returns True on hit.

    Table-of-contents entries are hyperlink blocks whose label text can be
    identical to the heading they point to, so link-bearing blocks are skipped
    when looking for the destination - otherwise a click would only scroll the
    TOC itself back into view at the top of the document.
    """
    display = anchor_map.get(fragment)
    if not display:
        return False
    block = browser.document().begin()
    while block.isValid():
        if not _block_has_hyperlink(block):
            text = block.text().strip()
            if text == display or text.startswith(display + " "):
                cursor = QTextCursor(block)
                browser.setTextCursor(cursor)
                browser.ensureCursorVisible()
                scrollbar = browser.verticalScrollBar()
                scrollbar.setValue(
                    max(0, scrollbar.value() + browser.cursorRect(cursor).top())
                )
                return True
        block = block.next()
    return False


class _TocTextBrowser(QTextBrowser):
    """A ``QTextBrowser`` that scrolls to headings for ``#slug`` links.

    The markdown renderer creates no named anchors, so QTextBrowser's built-in
    handling of same-document fragments finds nothing and silently dead-ends
    (and it never emits ``anchorClicked``). The click is therefore intercepted
    in the widget's own ``mouseReleaseEvent``, which is what a real OS click
    reaches. External links (with a scheme) fall through to the default
    ``openExternalLinks`` behaviour.
    """

    navigate: Callable[[str], bool] | None = None

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            href = self.anchorAt(event.position().toPoint())
            if href:
                url = QUrl(href)
                if (
                    not url.scheme()
                    and url.fragment()
                    and self.navigate
                    and self.navigate(url.fragment())
                ):
                    event.accept()
                    return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            fmt = self.textCursor().charFormat()
            href = fmt.anchorHref() or ""
            url = QUrl(href)
            if (
                not url.scheme()
                and url.fragment()
                and self.navigate
                and self.navigate(url.fragment())
            ):
                event.accept()
                return
        super().keyPressEvent(event)


class MarkdownViewerDialog(QDialog):
    """Modal dialog that loads a Markdown file from ``resources/docs/`` and
    renders it as rich text in a read-only ``QTextBrowser``.

    Table-of-contents links (``[x](#slug)``) are intercepted so that clicking
    one scrolls straight to the matching heading instead of doing nothing.
    """

    def __init__(self, filename: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(640, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        browser = _TocTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setReadOnly(True)
        browser.navigate = lambda fragment: _jump_to_heading(
            browser, self._anchor_map, fragment
        )

        self._browser = browser
        self._anchor_map: dict[str, str] = {}

        path = resource_path("docs", filename)
        if path.is_file():
            markdown = path.read_text(encoding="utf-8")
            rendered, self._anchor_map = _preprocess(markdown)
            browser.setMarkdown(rendered)
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