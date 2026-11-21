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

# Optional pinned public key for store-published Chromium extension builds.
# Commit only a PUBLIC key (PEM SPKI or base64 SPKI text). Never commit a
# private key. When present, this key defines the fixed Chromium extension ID
# used for store packages and native host allowed_origins.
_PINNED_CHROMIUM_PUBLIC_KEY_PATH = Path(__file__).with_name("chromium_store_public_key.pem")
_PINNED_CHROMIUM_PUBLIC_KEY_B64_PATH = Path(__file__).with_name("chromium_store_public_key.b64")

# Published store extension IDs (CRX/extension IDs) assigned by browser stores.
# These differ from the local pinned/dev key IDs because store uploads cannot
# include a manifest "key". Add new store IDs here when listings are published,
# then ship an app update and have users reinstall/update the native host.
PUBLISHED_CHROMIUM_EXTENSION_IDS: dict[str, str] = {
    "edge": "ahhbmbfafmcephmfghajdhpinamagaci",
    "opera": "gohjakpigbhmlgldfckbomlkinejhpkc",
    # "chrome": "<chrome-web-store-extension-id>",
}

FIREFOX_EXTENSION_ID = "magneto-companion@magnetoclip.app"


def extension_id_from_public_key(public_key: rsa.RSAPublicKey) -> str:
    """Compute the Chrome extension id from an RSA public key.

    Mirrors Chromium's ``GenerateIdForPath`` for keyed extensions: hash the
    DER SPKI with SHA-256, take the first 16 bytes and map each byte to two
    chars (high nibble first) using the alphabet ``abcdefghijklmnop``.
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
        chars.append(_EXTENSION_ALPHABET[byte >> 4])
        chars.append(_EXTENSION_ALPHABET[byte & 0x0F])
    return "".join(chars)


def public_key_base64(public_key: rsa.RSAPublicKey) -> str:
    """Base64 (no line breaks) of the DER SPKI, as Chrome's manifest ``key``."""
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(spki).decode("ascii")


# Parsing a 2048-bit PEM is ~65ms, and the Browser page asks for the extension
# id once per installed browser every time it is opened. The key is stable on
# disk, so cache the parsed key. The mtime/size are part of the key, so rotating
# the file still picks up the new one.
_KEY_CACHE: dict[tuple[str, int, int], rsa.RSAPublicKey] = {}


def _load_public_key_bytes(data: bytes) -> rsa.RSAPublicKey:
    text = data.decode("utf-8", errors="ignore").strip()
    if "-----BEGIN" in text:
        key = serialization.load_pem_public_key(text.encode("utf-8"))
    else:
        # Accept base64-encoded DER SPKI (same value as manifest "key").
        raw = "".join(text.split())
        key = serialization.load_der_public_key(base64.b64decode(raw))
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("Chromium extension store key must be an RSA public key")
    return key


def load_pinned_chromium_public_key() -> tuple[rsa.RSAPublicKey, Path] | None:
    """Load the pinned store/public Chromium extension key if configured.

    This should be a PUBLIC key only (safe to commit). It defines the fixed
    extension ID used by store-published Chromium builds and native host
    allowed_origins.
    """
    for path in (_PINNED_CHROMIUM_PUBLIC_KEY_PATH, _PINNED_CHROMIUM_PUBLIC_KEY_B64_PATH):
        if not path.is_file():
            continue
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _KEY_CACHE.get(cache_key)
        if cached is not None:
            return cached, path
        with open(path, "rb") as handle:
            public_key = _load_public_key_bytes(handle.read())
        _KEY_CACHE[cache_key] = public_key
        return public_key, path
    return None


def ensure_extension_key(data_dir: Path) -> tuple[Path, rsa.RSAPublicKey]:
    """Load or create the per-user dev signing key for unpacked Chromium builds.

    Store publishing should use a pinned public key (see
    load_pinned_chromium_public_key) so the extension ID is fixed.
    """
    private_path = data_dir / "browser" / "extension.pem"
    private_path.parent.mkdir(parents=True, exist_ok=True)
    if private_path.exists():
        stat = private_path.stat()
        cache_key = (str(private_path), stat.st_mtime_ns, stat.st_size)
        cached = _KEY_CACHE.get(cache_key)
        if cached is not None:
            return private_path, cached
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
    public_key = private_key.public_key()
    try:
        stat = private_path.stat()
        _KEY_CACHE[(str(private_path), stat.st_mtime_ns, stat.st_size)] = public_key
    except OSError:
        pass
    return private_path, public_key


def get_chromium_extension_public_keys(
    data_dir: Path, *, include_dev_key: bool = True
) -> list[tuple[rsa.RSAPublicKey, str]]:
    """Return Chromium extension public keys and their source labels.

    Includes the pinned store key when configured (preferred/fixed ID) and
    optionally the per-user dev key for unpacked/local compatibility.
    """
    keys: list[tuple[rsa.RSAPublicKey, str]] = []
    seen_ids: set[str] = set()

    pinned = load_pinned_chromium_public_key()
    if pinned is not None:
        public_key, _path = pinned
        ext_id = extension_id_from_public_key(public_key)
        keys.append((public_key, "store"))
        seen_ids.add(ext_id)

    if include_dev_key:
        try:
            _key_path, dev_key = ensure_extension_key(data_dir)
            dev_id = extension_id_from_public_key(dev_key)
            if dev_id not in seen_ids:
                keys.append((dev_key, "dev"))
        except Exception:  # noqa: BLE001 - dev key is best-effort fallback
            pass

    return keys


def get_primary_chromium_extension_id(data_dir: Path) -> str:
    """Return the preferred Chromium extension ID (pinned store ID if configured)."""
    keys = get_chromium_extension_public_keys(data_dir, include_dev_key=True)
    if not keys:
        raise RuntimeError("No Chromium extension key available")
    return extension_id_from_public_key(keys[0][0])


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
    if browser == "opera":
        # Opera Stable native messaging hosts (Windows). This path/registration
        # should be verified against the installed Opera build if issues arise.
        return Path(app_data) / "Opera Software" / "Opera Stable" / "NativeMessagingHosts" / f"{HOST_NAME}.json"
    if browser == "firefox":
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
    if browser == "opera":
        return "Opera Software\\Opera Stable\\NativeMessagingHosts"
    return "Google\\Chrome\\NativeMessagingHosts"


def _native_hosts_registry_key(browser: str) -> str | None:
    r"""HKCU registry path where a browser looks up native host manifests.

    Although macOS/Linux Firefox reads manifests from a fixed filesystem
    directory, on Windows it is registry-only: ``NativeManifests._winLookup``
    checks ``HKCU\Software\Mozilla\NativeMessagingHosts\<name>`` (then HKLM)
    and never scans ``%APPDATA%\Mozilla\NativeMessagingHosts\``. Writing just
    the manifest file there makes native messaging appear to work on macOS/
    Linux but silently fail on Windows, so Firefox must get a registry key.
    """
    if browser == "firefox":
        return "Software\\Mozilla\\NativeMessagingHosts"
    return f"Software\\{_browser_native_dir(browser)}"


def _register_native_host(browser: str, manifest_path: Path) -> None:
    key = _native_hosts_registry_key(browser)
    if key is None or winreg is None:
        return
    full_key = f"{key}\\{HOST_NAME}"
    # Firefox's Windows lookup reads HKCU with WOW64_64 first (then HKLM).
    # Without KEY_WOW64_64KEY a 32-bit app would write into Wow6432Node and
    # Firefox would never see it.
    access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, full_key, 0, access) as hkey:
        winreg.SetValue(hkey, "", winreg.REG_SZ, str(manifest_path))


def _unregister_native_host(browser: str) -> None:
    key = _native_hosts_registry_key(browser)
    if key is None or winreg is None:
        return
    access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, access) as parent:
            winreg.DeleteKey(parent, HOST_NAME)
    except OSError:
        pass


def build_host_manifest(
    host_path: Path, extension_id: str, browser: str, data_dir: Path
) -> dict:
    """Build a native messaging host manifest.

    `extension_id` is kept for API compatibility and is used as a fallback; for
    Chromium browsers we allow all known IDs (pinned store ID plus per-user dev
    ID when available) so store and unpacked/dev installs can both connect.
    """
    manifest: dict = {
        "name": HOST_NAME,
        "description": "MagnetoClip native messaging host",
        "path": str(host_path),
        "type": "stdio",
    }
    if browser == "firefox":
        manifest["allowed_extensions"] = [FIREFOX_EXTENSION_ID]
    else:
        ids: list[str] = []
        try:
            for public_key, _source in get_chromium_extension_public_keys(
                data_dir, include_dev_key=True
            ):
                ext_id = extension_id_from_public_key(public_key)
                if ext_id not in ids:
                    ids.append(ext_id)
        except Exception:  # noqa: BLE001 - fall back to provided id
            ids = []
        if extension_id and extension_id not in ids:
            ids.append(extension_id)
        for published_id in PUBLISHED_CHROMIUM_EXTENSION_IDS.values():
            normalized = str(published_id or "").strip().lower()
            if normalized and normalized not in ids:
                ids.append(normalized)
        manifest["allowed_origins"] = [f"chrome-extension://{eid}/" for eid in ids]
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
    try:
        extension_id = get_primary_chromium_extension_id(data_dir)
    except Exception:
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
