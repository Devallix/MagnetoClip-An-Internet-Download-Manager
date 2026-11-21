"""Add Torrent dialog — uTorrent-style: shows torrent info before adding.

Supports:
- Magnet URI pasted into the text field
- .torrent URL pasted into the text field
- .torrent file uploaded from disk via Browse button
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.torrent.detect import is_magnet_link, is_torrent_file_url
from magnetoclip.torrent.parser import TorrentMeta, parse_magnet_uri, parse_torrent_file


class AddTorrentDialog(QDialog):
    """Dialog that shows torrent metadata before adding — mirrors uTorrent behaviour."""

    def __init__(self, context, parent=None, *, url: str = "") -> None:
        super().__init__(parent)
        self.context = context
        self._meta: TorrentMeta | None = None
        self._torrent_file_path: str | None = None
        self.setWindowTitle("Add Torrent")
        self.setMinimumSize(600, 520)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # --- Source row: text field + Browse button ---
        source_group = QGroupBox("Torrent Source")
        source_lay = QVBoxLayout(source_group)
        source_lay.setSpacing(6)

        input_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Paste magnet URI or .torrent URL here..."
        )
        self.url_edit.setText(url)
        self.url_edit.textChanged.connect(self._on_url_changed)
        self.url_edit.returnPressed.connect(self._load_from_url)
        input_row.addWidget(self.url_edit, 1)

        self.load_button = QPushButton("Load")
        self.load_button.clicked.connect(self._load_from_url)
        input_row.addWidget(self.load_button)

        self.browse_file_button = QPushButton("Browse file...")
        self.browse_file_button.clicked.connect(self._browse_torrent_file)
        input_row.addWidget(self.browse_file_button)
        source_lay.addLayout(input_row)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #d44;")
        self.error_label.setVisible(False)
        source_lay.addWidget(self.error_label)
        root.addWidget(source_group)

        # --- Info panel (shown after loading) ---
        self.info_widget = QWidget()
        info_lay = QVBoxLayout(self.info_widget)
        info_lay.setContentsMargins(0, 0, 0, 0)
        info_lay.setSpacing(6)

        # Summary labels
        summary = QHBoxLayout()
        summary.setSpacing(24)
        self.name_label = QLabel("Name: —")
        self.name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary.addWidget(self.name_label, 1)
        self.size_label = QLabel("Size: —")
        summary.addWidget(self.size_label)
        self.tracker_label = QLabel("Tracker: —")
        summary.addWidget(self.tracker_label, 1)
        info_lay.addLayout(summary)

        hash_row = QHBoxLayout()
        self.hash_label = QLabel("Info hash: —")
        self.hash_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hash_row.addWidget(self.hash_label, 1)
        self.files_count_label = QLabel("Files: 0")
        hash_row.addWidget(self.files_count_label)
        info_lay.addLayout(hash_row)

        # File tree table
        self.file_table = QTableWidget(0, 2)
        self.file_table.setHorizontalHeaderLabels(["File", "Size"])
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.verticalHeader().setDefaultSectionSize(24)
        self.file_table.setShowGrid(False)
        self.file_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.file_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.file_table.setFocusPolicy(Qt.NoFocus)
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        info_lay.addWidget(self.file_table, 1)

        root.addWidget(self.info_widget, 1)
        self.info_widget.setVisible(False)

        # --- Options ---
        opts_group = QGroupBox("Options")
        opts_lay = QVBoxLayout(opts_group)
        opts_lay.setSpacing(6)

        dir_row = QHBoxLayout()
        self.directory_edit = QLineEdit()
        torrent_dir = context.settings.get("torrent.default_save_dir", "")
        self.directory_edit.setPlaceholderText(
            torrent_dir
            or context.settings.get("downloads.default_directory", "")
            or str(Path.home() / "Downloads")
        )
        self.dir_browse_button = QPushButton("Browse...")
        self.dir_browse_button.clicked.connect(self._browse_directory)
        dir_row.addWidget(QLabel("Save to"))
        dir_row.addWidget(self.directory_edit, 1)
        dir_row.addWidget(self.dir_browse_button)
        opts_lay.addLayout(dir_row)

        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("Category"))
        self.category_combo = QComboBox()
        for cat in getattr(getattr(context, "categories", None), "list", list)():
            self.category_combo.addItem(cat.name)
        if self.category_combo.count() == 0:
            self.category_combo.addItem("Other")
        torrent_idx = self.category_combo.findText("Torrent")
        if torrent_idx >= 0:
            self.category_combo.setCurrentIndex(torrent_idx)
        cat_row.addWidget(self.category_combo)
        cat_row.addStretch(1)
        opts_lay.addLayout(cat_row)

        checks = QHBoxLayout()
        checks.setSpacing(16)
        self.sequential_check = QCheckBox("Sequential download")
        self.sequential_check.setChecked(
            bool(context.settings.get("torrent.default_sequential", False))
        )
        checks.addWidget(self.sequential_check)
        self.seed_check = QCheckBox("Seed after download")
        self.seed_check.setChecked(
            bool(context.settings.get("torrent.auto_seed", False))
        )
        checks.addWidget(self.seed_check)
        checks.addStretch(1)
        opts_lay.addLayout(checks)
        root.addWidget(opts_group)

        # --- Buttons ---
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Auto-load if URL was provided
        if url:
            self._load_from_url()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_from_url(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            return
        error = self._url_error(url)
        if error:
            self.error_label.setText(error)
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)

        if is_magnet_link(url):
            self._meta = parse_magnet_uri(url)
            self._show_meta()
        elif Path(url).is_file() and url.lower().endswith(".torrent"):
            self._torrent_file_path = url
            try:
                self._meta = parse_torrent_file(url)
                self._meta.source = "file"
                self._show_meta()
            except Exception as exc:
                self.error_label.setText(f"Failed to parse torrent: {exc}")
                self.error_label.setVisible(True)
        elif is_torrent_file_url(url):
            self._download_and_parse(url)
        elif url.lower().startswith(("http://", "https://")):
            self._download_and_parse(url)

    def _download_and_parse(self, url: str) -> None:
        """Download a .torrent file from a URL, parse it, and show metadata."""
        import httpx

        self.error_label.setVisible(False)
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.content
        except Exception as exc:
            self.error_label.setText(f"Failed to download: {exc}")
            self.error_label.setVisible(True)
            return

        from magnetoclip.torrent.parser import _parse_torrent_bytes

        self._meta = _parse_torrent_bytes(data)
        self._meta.source = "url"
        self._show_meta()

    def _browse_torrent_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open .torrent File", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        if not path:
            return
        self._torrent_file_path = path
        self.url_edit.setText(path)
        self.error_label.setVisible(False)
        try:
            self._meta = parse_torrent_file(path)
            self._meta.source = "file"
            self._show_meta()
        except Exception as exc:
            self.error_label.setText(f"Failed to parse torrent: {exc}")
            self.error_label.setVisible(True)

    def _show_meta(self) -> None:
        """Populate the info panel from self._meta."""
        meta = self._meta
        if meta is None:
            return
        self.info_widget.setVisible(True)

        self.name_label.setText(f"Name: {meta.name or '—'}")
        self.size_label.setText(f"Size: {meta.size_text}")
        self.tracker_label.setText(f"Tracker: {meta.tracker_url or '—'}")
        self.hash_label.setText(f"Info hash: {meta.info_hash or '—'}")
        self.files_count_label.setText(f"Files: {meta.file_count}")

        # Populate file table
        self.file_table.setRowCount(0)
        if meta.files:
            for fi in meta.files:
                row = self.file_table.rowCount()
                self.file_table.insertRow(row)
                self.file_table.setItem(row, 0, QTableWidgetItem(fi.path))
                self.file_table.setItem(row, 1, QTableWidgetItem(_human_size(fi.size)))
        else:
            row = 0
            self.file_table.insertRow(row)
            self.file_table.setItem(row, 0, QTableWidgetItem(meta.name or "download"))
            self.file_table.setItem(row, 1, QTableWidgetItem(meta.size_text))

        # Auto-fill save dir placeholder with torrent name
        if meta.name and not self.directory_edit.text():
            default = (
                self.context.settings.get("torrent.default_save_dir", "")
                or self.context.settings.get("downloads.default_directory", "")
                or str(Path.home() / "Downloads")
            )
            self.directory_edit.setPlaceholderText(
                str(Path(default) / meta.name)
            )

    # ------------------------------------------------------------------
    # Validation & accept
    # ------------------------------------------------------------------

    def _accept(self) -> None:
        url = self.url_edit.text().strip()
        if not url and not self._torrent_file_path:
            self.url_edit.setFocus()
            return
        if url and not self._meta:
            self._load_from_url()
            if not self._meta:
                return
        self.accept()

    @staticmethod
    def _url_error(url: str) -> str | None:
        if is_magnet_link(url) or is_torrent_file_url(url):
            return None
        lower = url.strip().lower()
        if lower.startswith(("http://", "https://")):
            return None
        if Path(url).is_file() and url.lower().endswith(".torrent"):
            return None
        return "Please enter a magnet URI, .torrent URL, or browse for a .torrent file."

    def _on_url_changed(self, _text: str) -> None:
        self.error_label.setVisible(False)
        self.info_widget.setVisible(False)
        self._meta = None

    # ------------------------------------------------------------------
    # Directory browse
    # ------------------------------------------------------------------

    def _browse_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose save folder")
        if directory:
            self.directory_edit.setText(directory)

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def url(self) -> str:
        if self._torrent_file_path:
            return self._torrent_file_path
        return self.url_edit.text().strip()

    def filename(self) -> str:
        if self._meta and self._meta.name:
            return self._meta.name
        return ""

    def directory(self) -> str:
        return self.directory_edit.text().strip() or None

    def category(self) -> str | None:
        return self.category_combo.currentText()

    def sequential(self) -> bool:
        return self.sequential_check.isChecked()

    def seed_mode(self) -> bool:
        return self.seed_check.isChecked()

    def torrent_file_path(self) -> str | None:
        return self._torrent_file_path

    def meta(self) -> TorrentMeta | None:
        return self._meta


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"
