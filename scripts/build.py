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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "scripts" / "magnetoclip.spec"
DIST = ROOT / "dist"
ISS = ROOT / "scripts" / "installer.iss"
RELEASES = ROOT / "releases"


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


def build_zip() -> Path:
    """Create a zip file from the dist/MagnetoClip/ folder.

    The zip contains MagnetoClip.exe and _internal/ folder at the root level.
    Returns the path to the created zip file.
    """
    import hashlib
    import json
    import re

    version_file = ROOT / "src" / "magnetoclip" / "version.py"
    with open(version_file, "r") as f:
        content = f.read()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    if not match:
        raise SystemExit("Could not read version from version.py")
    version = match.group(1)

    app_dir = DIST / "MagnetoClip"
    if not app_dir.exists():
        raise SystemExit(f"Build directory not found: {app_dir}")

    RELEASES.mkdir(parents=True, exist_ok=True)
    zip_name = f"MagnetoClip-{version}.zip"
    zip_path = RELEASES / zip_name

    if zip_path.exists():
        zip_path.unlink()

    print(f"Creating {zip_name}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        exe_path = app_dir / "MagnetoClip.exe"
        if exe_path.exists():
            zf.write(exe_path, "MagnetoClip.exe")
            print(f"  + MagnetoClip.exe")

        internal_dir = app_dir / "_internal"
        if internal_dir.exists():
            for file_path in internal_dir.rglob("*"):
                if file_path.is_file():
                    arcname = f"_internal/{file_path.relative_to(internal_dir)}"
                    zf.write(file_path, arcname)
            print(f"  + _internal/")

    sha256 = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    sha256_hex = sha256.hexdigest()

    size_bytes = zip_path.stat().st_size

    manifest_path = RELEASES / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    else:
        manifest = {}

    manifest["version"] = version
    manifest["filename"] = zip_name
    manifest["size_bytes"] = size_bytes
    manifest["sha256"] = sha256_hex

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    print(f"OK: {zip_path}")
    print(f"  SHA256: {sha256_hex}")
    print(f"  Size: {size_bytes:,} bytes")
    print(f"  Manifest updated: {manifest_path}")
    return zip_path


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
    parser.add_argument("--zip", action="store_true", default=True, help="create release zip (default: True)")
    parser.add_argument("--no-zip", action="store_true", help="skip zip creation")
    args = parser.parse_args()

    build_app()
    if args.installer:
        build_installer()
    if args.zip and not args.no_zip:
        build_zip()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
