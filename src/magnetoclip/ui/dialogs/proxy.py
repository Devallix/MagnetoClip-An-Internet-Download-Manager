"""Proxy profile management dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


class ProxyDialog(QDialog):
    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("Manage Proxy Profiles")
        self.setMinimumSize(520, 420)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Saved profiles"))
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        buttons_row = QHBoxLayout()
        self.add_button = QPushButton("Add")
        self.remove_button = QPushButton("Remove selected")
        self.add_button.clicked.connect(self._add)
        self.remove_button.clicked.connect(self._remove_selected)
        buttons_row.addWidget(self.add_button)
        buttons_row.addWidget(self.remove_button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        self._reload()

        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        box.clicked.connect(self.reject)
        layout.addWidget(box)

    def _reload(self) -> None:
        self.list_widget.clear()
        for profile in self.context.proxies.list():
            target = f"{profile.host}:{profile.port}" if profile.host else "direct"
            self.list_widget.addItem(f"{profile.name}  ({profile.type} — {target})")

    def _add(self) -> None:
        dialog = _ProxyFormDialog(self.context, parent=self)
        if dialog.exec():
            try:
                self.context.proxies.add(
                    dialog.name(),
                    proxy_type=dialog.proxy_type(),
                    host=dialog.host() or None,
                    port=dialog.port() or None,
                    username_ref=dialog.username() or None,
                )
            except ValueError as exc:
                QMessageBox.warning(self, "Proxy", str(exc))
                return
            self._reload()

    def _remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        profile = self.context.proxies.list()[row]
        self.context.proxies.remove(profile.id)
        self._reload()


class _ProxyFormDialog(QDialog):
    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Proxy Profile")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit()
        form.addRow("Name", self.name_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(["http", "https", "socks5"])
        form.addRow("Type", self.type_combo)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("proxy.example.com")
        form.addRow("Host", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8080)
        form.addRow("Port", self.port_spin)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("optional")
        form.addRow("Username (keyring)", self.username_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("optional")
        form.addRow("Password (keyring)", self.password_edit)

        layout.addLayout(form)

        box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        self.accept()

    def name(self) -> str:
        return self.name_edit.text().strip()

    def proxy_type(self) -> str:
        return self.type_combo.currentText()

    def host(self) -> str:
        return self.host_edit.text().strip()

    def port(self) -> int:
        return self.port_spin.value()

    def username(self) -> str:
        return self.username_edit.text().strip()

    def password(self) -> str:
        return self.password_edit.text()
