"""Generate app icon assets from the source logo.

Produces src/magnetoclip/resources/icons/logo.ico (256x256) and a square
logo.png from the original artwork. Run from the project root:

    python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageWriter, QPainter

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "img" / "logo.png"
OUT_DIR = ROOT / "src" / "magnetoclip" / "resources" / "icons"
ICON_SIZE = 256


def make_square(source: QImage, size: int) -> QImage:
    """Aspect-fit the artwork into a transparent square canvas."""
    canvas = QImage(QSize(size, size), QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    scaled = source.scaled(
        QSize(size, size),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawImage(x, y, scaled)
    painter.end()
    return canvas


def main() -> int:
    if not SOURCE.exists():
        print(f"source logo not found: {SOURCE}")
        return 1
    source = QImage(str(SOURCE))
    if source.isNull():
        print(f"failed to load {SOURCE}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    square = make_square(source, ICON_SIZE)

    ico_path = OUT_DIR / "logo.ico"
    writer = QImageWriter(str(ico_path), b"ICO")
    if not writer.write(square):
        print(f"failed to write {ico_path}: {writer.errorString()}")
        return 1

    png_path = OUT_DIR / "logo.png"
    if not square.save(str(png_path), "PNG"):
        print(f"failed to write {png_path}")
        return 1

    print(f"wrote {ico_path} and {png_path} ({ICON_SIZE}x{ICON_SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
