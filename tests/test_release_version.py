"""Guards on the release artifacts' version numbers.

The app version is declared once, in ``src/magnetoclip/version.py``, but the
zip name, the manifest and the Inno Setup script each carry their own copy. A
mismatch ships an installer labelled with the wrong version, which is hard to
notice and worse to undo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from magnetoclip.version import __version__

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "magnetoclip" / "version.py"
INSTALLER_ISS = ROOT / "scripts" / "installer.iss"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_version_file_is_the_source_of_truth():
    match = re.search(r'__version__\s*=\s*"([^"]+)"', _read(VERSION_FILE))

    assert match, "could not read __version__"
    assert match.group(1) == __version__


def test_installer_version_matches_the_app():
    match = re.search(r'#define MyAppVersion\s+"([^"]+)"', _read(INSTALLER_ISS))

    assert match, "could not read MyAppVersion from installer.iss"
    assert match.group(1) == __version__, (
        "scripts/installer.iss and src/magnetoclip/version.py disagree; "
        "the installer would be labelled with the wrong version"
    )


def test_installer_packages_the_bundled_ffmpeg():
    """The whole dist tree is packaged recursively, so ffmpeg must be in it."""
    content = _read(INSTALLER_ISS)

    # Paths must be anchored on ProjectRoot, because Inno Setup resolves
    # relative Source paths against the .iss directory rather than the cwd.
    assert 'Source: "{#ProjectRoot}\\dist\\MagnetoClip\\*"' in content
    assert "recursesubdirs" in content
    # And there must be a default so a manual ISCC invocation still works.
    assert '#define ProjectRoot ".."' in content


def test_installer_avoids_nested_preprocessor_defines():
    """ISCC substitutes #define values literally, so nesting would not expand."""
    content = _read(INSTALLER_ISS)

    for line in content.splitlines():
        if line.startswith("#define") and "{#" in line:
            pytest.fail(f"nested preprocessor reference in: {line.strip()}")


def test_installer_output_lands_beside_the_app():
    content = _read(INSTALLER_ISS)

    assert "OutputDir={#ProjectRoot}\\dist" in content


@pytest.mark.skipif(
    not (ROOT / "releases" / "manifest.json").is_file(),
    reason="no release manifest yet",
)
def test_release_manifest_matches_the_app():
    manifest = json.loads(_read(ROOT / "releases" / "manifest.json"))

    assert manifest["version"] == __version__
    assert manifest["filename"] == f"MagnetoClip-{__version__}.zip"
