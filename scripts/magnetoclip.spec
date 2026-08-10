# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for MagnetoClip.

Build:  python -m PyInstaller --noconfirm --clean scripts/magnetoclip.spec
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


def _find_project_root() -> Path:
    """Walk up from this spec's directory to the repo root (has pyproject.toml)."""
    current = Path.cwd()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "scripts").exists():
            return candidate
    raise RuntimeError("could not locate project root from cwd")


ROOT = _find_project_root()
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

hiddenimports = [
    "qasync",
    *collect_submodules("qasync"),
    *collect_submodules("keyring.backends"),
    *collect_submodules("httpx"),
    "aiofiles",
]

datas = []
for pattern in ("*.qss",):
    datas += collect_data_files("magnetoclip.ui.themes", includes=[pattern])
datas.append(
    (str(ROOT / "src" / "magnetoclip" / "resources" / "browser_extension"),
     "magnetoclip/resources/browser_extension")
)
datas.append(
    (str(ROOT / "src" / "magnetoclip" / "resources" / "icons"),
     "magnetoclip/resources/icons")
)
datas.append(
    (str(ROOT / "img"),
     "img")
)
ICON_PATH = str(ROOT / "src" / "magnetoclip" / "resources" / "icons" / "logo.ico")

a = Analysis(
    [str(ROOT / "src" / "magnetoclip" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MagnetoClip",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=ICON_PATH,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MagnetoClip",
)
