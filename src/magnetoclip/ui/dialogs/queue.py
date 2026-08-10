"""Create / edit download queue dialog: name plus concurrency limit."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)


class QueueDialog(QDialog):
    def __init__(self, queue=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Queue" if queue is not None else "New Queue")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Work downloads")
        if queue is not None:
            self.name_edit.setText(queue.name)
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Max concurrent downloads"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 20)
        self.concurrency_spin.setValue(queue.max_concurrent if queue is not None else 3)
        layout.addWidget(self.concurrency_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if self.name_edit.text().strip():
            self.accept()

    def name(self) -> str:
        return self.name_edit.text().strip()

    def max_concurrent(self) -> int:
        return self.concurrency_spin.value()
