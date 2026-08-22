"""New download dialog: URL plus optional filename, category, queue, options."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from magnetoclip.media.streaming import is_audio_platform, is_streaming_url
from magnetoclip.torrent.detect import is_torrent_url


class AddUrlDialog(QDialog):
    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("New Download")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("URL"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/file.zip")
        self.url_edit.textChanged.connect(self._on_url_changed)
        layout.addWidget(self.url_edit)

        self.blob_hint = QLabel(
            "blob: URLs (image/video addresses copied from a page) are fetched "
            "from the browser with the MagnetoClip extension."
        )
        self.blob_hint.setWordWrap(True)
        self.blob_hint.setObjectName("muted")
        self.blob_hint.setVisible(False)
        layout.addWidget(self.blob_hint)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #d44;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        layout.addWidget(QLabel("Filename (optional)"))
        self.filename_edit = QLineEdit()
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
        self._categories = {}
        for category in getattr(getattr(context, "categories", None), "list", list)():
            self._categories[category.name] = category
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

        layout.addWidget(QLabel("Proxy profile"))
        self.proxy_combo = QComboBox()
        self.proxy_combo.addItem("Direct (no proxy)", 0)
        for profile in getattr(getattr(context, "proxies", None), "list", list)():
            self.proxy_combo.addItem(profile.name, profile.id)
        default_id = int(context.settings.get("network.default_proxy_id", 0) or 0)
        index = self.proxy_combo.findData(default_id)
        if index >= 0:
            self.proxy_combo.setCurrentIndex(index)
        layout.addWidget(self.proxy_combo)

        auth_row = QHBoxLayout()
        auth_row.setSpacing(8)
        self.auth_username_edit = QLineEdit()
        self.auth_username_edit.setPlaceholderText("Basic auth username")
        self.auth_password_edit = QLineEdit()
        self.auth_password_edit.setPlaceholderText("Password")
        self.auth_password_edit.setEchoMode(QLineEdit.Password)
        auth_row.addWidget(self.auth_username_edit, 1)
        auth_row.addWidget(self.auth_password_edit, 1)
        layout.addLayout(auth_row)

        layout.addWidget(QLabel("Cookies (name=value; one per line, optional)"))
        self.cookies_edit = QLineEdit()
        self.cookies_edit.setPlaceholderText("session=abc123")
        layout.addWidget(self.cookies_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.url_edit.returnPressed.connect(self._accept)

    def _accept(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.url_edit.setFocus()
            return
        error = self._url_error(url)
        if error:
            self.error_label.setText(error)
            self.error_label.setVisible(True)
            self.url_edit.setFocus()
            return
        self.accept()

    @staticmethod
    def _url_error(url: str) -> str | None:
        """Return a friendly message for URLs MagnetoClip cannot download."""
        if url.lower().startswith("blob:"):
            # blob: URLs only exist inside a browser page; MagnetoClip asks the
            # extension to fetch the bytes from the page that created them.
            return None
        if is_torrent_url(url):
            return None
        try:
            parsed = httpx.URL(url)
        except Exception:
            return "That doesn't look like a valid URL."
        if parsed.scheme not in ("http", "https") or not parsed.host:
            return "Only http://, https://, blob:, and magnet: URLs are supported."
        return None

    def _on_url_changed(self, text: str) -> None:
        """Auto-select a category from the URL."""
        self.error_label.setVisible(False)
        self.blob_hint.setVisible(text.strip().lower().startswith("blob:"))
        categories = getattr(self.context, "categories", None)
        if categories is None or not hasattr(categories, "classify"):
            return
        url = text.strip()
        if not url:
            return
        name: str | None = None
        if is_streaming_url(url):
            # Streaming platforms resolve to video/audio media, never to a
            # plain file, so their category is determined by the host.
            name = "Music" if is_audio_platform(url) else "Videos"
        else:
            basename = Path(urlsplit(url).path).name
            extension = Path(basename).suffix.lower().lstrip(".")
            if not extension:
                # Extensionless URLs often carry the format in the query, e.g.
                # ``?format=jpg`` (Twitter image CDN).
                fmt = (parse_qs(urlsplit(url).query).get("format") or [""])[0]
                if fmt:
                    extension = fmt.lower().lstrip(".")
                    basename = f"{basename or 'file'}.{extension}"
            if basename:
                category = categories.classify(filename=basename, url=url)
                if category is not None:
                    name = category.name
        if name:
            index = self.category_combo.findText(name)
            if index >= 0:
                self.category_combo.setCurrentIndex(index)

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose destination folder"
        )
        if directory:
            self.directory_edit.setText(directory)

    def url(self) -> str:
        return self.url_edit.text().strip()

    def filename(self) -> str:
        return self.filename_edit.text().strip()

    def directory(self) -> str:
        return self.directory_edit.text().strip() or None

    def category(self) -> str | None:
        return self.category_combo.currentText()

    def connections(self) -> int | None:
        return self.connections_spin.value()

    def proxy_profile_id(self) -> int | None:
        value = self.proxy_combo.currentData()
        return value or None

    def auth_username(self) -> str | None:
        return self.auth_username_edit.text().strip() or None

    def auth_password(self) -> str | None:
        return self.auth_password_edit.text()

    def cookies(self) -> dict[str, str] | None:
        text = self.cookies_edit.text().strip()
        if not text:
            return None
        from magnetoclip.network.cookies.jar import parse_cookie_header

        return parse_cookie_header(text)
