"""One-command Windows build: PyInstaller bundle + optional Inno installer.

Usage:
    python scripts/build.py            # build the frozen app in dist/
    python scripts/build.py --installer  # also compile the Inno Setup installer
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "scripts" / "magnetoclip.spec"
DIST = ROOT / "dist"
ISS = ROOT / "scripts" / "installer.iss"


def _require_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Installing...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True,
        )


def build_app() -> None:
    _require_pyinstaller()
    if DIST.exists():
        shutil.rmtree(DIST)
    print(f"Building with {SPEC.name}...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=ROOT,
        check=True,
    )
    exe = DIST / "MagnetoClip" / "MagnetoClip.exe"
    if not exe.exists():
        raise SystemExit(f"Build finished but {exe} was not produced.")
    print(f"OK: {exe}")


def build_installer() -> None:
    import shutil

    iscc = shutil.which("ISCC.exe")
    if iscc is None:
        print(
            "Inno Setup not found on PATH. Install it from "
            "https://jrsoftware.org/isinfo.php and add ISCC.exe to PATH."
        )
        raise SystemExit(1)
    print("Compiling installer...")
    subprocess.run([iscc, str(ISS)], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build MagnetoClip")
    parser.add_argument("--installer", action="store_true", help="also run Inno Setup")
    args = parser.parse_args()

    build_app()
    if args.installer:
        build_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
