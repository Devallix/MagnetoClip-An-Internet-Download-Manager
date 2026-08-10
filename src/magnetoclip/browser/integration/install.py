"""Install/uninstall the native messaging host and extension for browsers."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from magnetoclip.services.logging import get_logger

log = get_logger(__name__)

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import winreg
else:
    winreg = None

HOST_NAME = "com.magnetoclip.host"
_EXTENSION_ALPHABET = "abcdefghijklmnop"

_CHROMIUM_APP_ID = "MagnetoClip"


def extension_id_from_public_key(public_key: rsa.RSAPublicKey) -> str:
    """Compute the Chrome extension id from an RSA public key.

    Mirrors Chromium's ``GenerateIdForPath``: hash the SPKI, take the first
    16 bytes and map each byte to two chars (low nibble first) using the
    alphabet ``abcdefghijklmnop``.
    """
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    first16 = digest.finalize()[:16]
    chars = []
    for byte in first16:
        chars.append(_EXTENSION_ALPHABET[byte & 0x0F])
        chars.append(_EXTENSION_ALPHABET[(byte >> 4) & 0x0F])
    return "".join(chars)


def public_key_base64(public_key: rsa.RSAPublicKey) -> str:
    """Base64 (no line breaks) of the DER SPKI, as Chrome's manifest ``key``."""
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(spki).decode("ascii")


def ensure_extension_key(data_dir: Path) -> tuple[Path, rsa.RSAPublicKey]:
    """Load or create the signing key used to derive a stable extension id."""
    private_path = data_dir / "browser" / "extension.pem"
    private_path.parent.mkdir(parents=True, exist_ok=True)
    if private_path.exists():
        with open(private_path, "rb") as handle:
            private_key = serialization.load_pem_private_key(
                handle.read(), password=None
            )
    else:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with open(private_path, "wb") as handle:
            handle.write(pem)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    return private_path, private_key.public_key()


def host_manifest_path(browser: str, data_dir: Path) -> Path:
    """Return the location where the host manifest must be written."""
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    app_data = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    if browser in ("chrome", "edge", "chromium", "brave", "vivaldi"):
        return (
            Path(local_app_data)
            / _browser_native_dir(browser)
            / f"{HOST_NAME}.json"
        )
    if browser in ("firefox",):
        return Path(app_data) / "Mozilla" / "NativeMessagingHosts" / f"{HOST_NAME}.json"
    raise ValueError(f"unsupported browser: {browser}")


def _browser_native_dir(browser: str) -> str:
    if browser == "edge":
        return "Microsoft\\Edge\\NativeMessagingHosts"
    if browser == "chromium":
        return "Chromium\\NativeMessagingHosts"
    if browser == "brave":
        return "BraveSoftware\\Brave-Browser\\NativeMessagingHosts"
    if browser == "vivaldi":
        return "Vivaldi\\NativeMessagingHosts"
    return "Google\\Chrome\\NativeMessagingHosts"


def _native_hosts_registry_key(browser: str) -> str | None:
    """HKCU registry path where a browser looks up native host manifests.

    Returns ``None`` for browsers using file-based discovery (Firefox).
    """
    if browser in ("firefox",):
        return None
    return f"Software\\{_browser_native_dir(browser)}"


def _register_native_host(browser: str, manifest_path: Path) -> None:
    key = _native_hosts_registry_key(browser)
    if key is None or winreg is None:
        return
    full_key = f"{key}\\{HOST_NAME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, full_key) as hkey:
        winreg.SetValue(hkey, "", winreg.REG_SZ, str(manifest_path))


def _unregister_native_host(browser: str) -> None:
    key = _native_hosts_registry_key(browser)
    if key is None or winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as parent:
            winreg.DeleteKey(parent, HOST_NAME)
    except OSError:
        pass


def build_host_manifest(
    host_path: Path, extension_id: str, browser: str, data_dir: Path
) -> dict:
    manifest: dict = {
        "name": HOST_NAME,
        "description": "MagnetoClip native messaging host",
        "path": str(host_path),
        "type": "stdio",
    }
    if browser == "firefox":
        manifest["allowed_extensions"] = ["magneto-companion@magnetoclip.app"]
    else:
        manifest["allowed_origins"] = [f"chrome-extension://{extension_id}/"]
    return manifest


def write_host_manifest(
    browser: str, host_path: Path, extension_id: str, data_dir: Path
) -> Path:
    manifest = build_host_manifest(host_path, extension_id, browser, data_dir)
    path = host_manifest_path(browser, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    log.info(
        "host_manifest_written",
        browser=browser,
        path=str(path),
        host=str(host_path),
        extension_id=extension_id,
    )
    return path


def install(
    browsers: list[str], host_path: Path, data_dir: Path
) -> dict[str, str]:
    """Register the host for each requested browser. Returns results per browser."""
    _key_path, public_key = ensure_extension_key(data_dir)
    extension_id = extension_id_from_public_key(public_key)
    results: dict[str, str] = {}
    for browser in browsers:
        try:
            path = write_host_manifest(browser, host_path, extension_id, data_dir)
            _register_native_host(browser, path)
            results[browser] = f"registered at {path}"
        except Exception as exc:
            log.exception("host_install_failed", browser=browser)
            results[browser] = f"failed: {exc}"
    return results


def uninstall(browsers: list[str], data_dir: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for browser in browsers:
        try:
            path = host_manifest_path(browser, data_dir)
            _unregister_native_host(browser)
            if path.exists():
                path.unlink()
                results[browser] = "removed"
            else:
                results[browser] = "not installed"
        except Exception as exc:  # noqa: BLE001 - report per-browser failures
            results[browser] = f"failed: {exc}"
    return results
