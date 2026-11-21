"""Helpers for preparing browser-extension packages for store submission.

This module centralizes manifest transforms used by both local browser integration
(prepared unpacked extension folders) and release/store packaging scripts.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

# Shared scripts loaded before background.js in both engine-specific manifests.
BACKGROUND_SHARED_SCRIPTS = ("compat.js", "categories.js", "media_detection_core.js", "queue.js")


def _load_pinned_chromium_public_key_base64() -> str:
    """Return base64 SPKI for the pinned Chromium store public key.

    Chromium store packages (Chrome/Edge/Opera) should use a pinned key so the
    extension ID is fixed and matches native host allowed_origins.
    """
    from .install import load_pinned_chromium_public_key, public_key_base64

    pinned = load_pinned_chromium_public_key()
    if pinned is None:
        raise FileNotFoundError(
            "Pinned Chromium store public key not found. Add a PUBLIC key at "
            "src/magnetoclip/browser/integration/chromium_store_public_key.pem "
            "(PEM SPKI) or chromium_store_public_key.b64 (base64 SPKI). "
            "Do not commit the private key."
        )
    public_key, _path = pinned
    return public_key_base64(public_key)


def prepare_firefox_manifest(manifest: dict) -> dict:
    """Return a Firefox-specific manifest derived from the bundled manifest.

    Firefox (our current supported target) needs a different background/action
    shape than Chromium MV3 service workers, and has a few Firefox-only
    permissions/content-script adjustments.
    """
    result = json.loads(json.dumps(manifest))

    result.pop("key", None)

    # ``world: MAIN`` in declarative content scripts is only supported by newer
    # Firefox versions. Keep the document_start bridge and rely on runtime/main
    # world hooks and the independent webRequest response-body detector.
    result["content_scripts"] = [
        item for item in result.get("content_scripts", []) if item.get("world") != "MAIN"
    ]

    # Firefox's response-body stream filter is a detection advantage for
    # extensionless HLS/DASH endpoints. These permissions are added only to the
    # Firefox build: Chromium MV3 does not grant webRequestBlocking to ordinary
    # extensions.
    permissions = list(result.get("permissions", []))
    for permission in ("webRequestBlocking", "webRequestFilterResponse"):
        if permission not in permissions:
            permissions.append(permission)
    result["permissions"] = permissions

    result["background"] = {"scripts": [*BACKGROUND_SHARED_SCRIPTS, "background.js"]}

    # Keep MV3 "action" for Firefox (lint warns on browser_action in MV3).
    # Ensure Gecko settings required/recommended for AMO are present.
    gecko_settings = result.setdefault("browser_specific_settings", {}).setdefault("gecko", {})
    gecko_settings.setdefault("id", "magneto-companion@magnetoclip.app")
    # data_collection_permissions (required by AMO for new submissions) is only
    # supported in newer Firefox versions; bump min version to satisfy lint/AMO.
    gecko_settings["strict_min_version"] = "142.0"
    # AMO requires data collection permissions metadata for new submissions.
    # Default to "none"; review and adjust if the extension's data practices differ.
    gecko_settings.setdefault("data_collection_permissions", {"required": ["none"]})

    return result


def prepare_chromium_manifest(
    manifest: dict, *, key_base64: str | None = None, for_store: bool = False
) -> dict:
    """Return a Chromium manifest derived from the bundled manifest.

    Store-upload packages (Chrome/Edge Web Stores) must not contain the `key`
    field; pass `for_store=True` to omit it. Unpacked/dev packages can include a
    pinned public key so the extension ID is fixed for native messaging.
    """
    result = json.loads(json.dumps(manifest))
    if for_store:
        result.pop("key", None)
    else:
        if not key_base64:
            raise ValueError("key_base64 is required for non-store Chromium manifests")
        result["key"] = key_base64
    result.pop("browser_specific_settings", None)
    if "browser_action" in result and "action" not in result:
        result["action"] = result.pop("browser_action")
    result["background"] = {"service_worker": "background.js"}
    return result


def _copy_if_changed(source: Path, destination: Path) -> None:
    try:
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            return
    except OSError:
        pass
    shutil.copy2(source, destination)


def _sync_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        child = destination / entry.name
        if entry.is_dir():
            _sync_tree(entry, child)
        else:
            _copy_if_changed(entry, child)


def _write_if_changed(path: Path, content: str) -> None:
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    path.write_text(content, encoding="utf-8")


def stage_firefox_extension(source_dir: Path, target_dir: Path) -> Path:
    """Copy bundled extension files into *target_dir* with a Firefox manifest."""
    target_dir.mkdir(parents=True, exist_ok=True)

    for entry in source_dir.iterdir():
        destination = target_dir / entry.name
        if entry.is_dir():
            _sync_tree(entry, destination)
        else:
            _copy_if_changed(entry, destination)

    manifest_path = target_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    firefox_manifest = prepare_firefox_manifest(manifest)
    _write_if_changed(manifest_path, json.dumps(firefox_manifest, indent=2) + "\n")
    return target_dir


def stage_chromium_extension(
    source_dir: Path,
    target_dir: Path,
    *,
    key_base64: str | None = None,
    for_store: bool = False,
) -> Path:
    """Copy bundled extension files into *target_dir* with a Chromium manifest.

    `for_store=True` produces a store-upload package (no manifest `key` field),
    required by Chrome/Edge Add-ons. Otherwise uses the pinned Chromium public
    key by default so the extension ID is fixed for unpacked/dev use and native
    messaging.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    key = None if for_store else (key_base64 or _load_pinned_chromium_public_key_base64())

    for entry in source_dir.iterdir():
        destination = target_dir / entry.name
        if entry.is_dir():
            _sync_tree(entry, destination)
        else:
            _copy_if_changed(entry, destination)

    manifest_path = target_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    chromium_manifest = prepare_chromium_manifest(
        manifest, key_base64=key, for_store=for_store
    )
    _write_if_changed(manifest_path, json.dumps(chromium_manifest, indent=2) + "\n")
    return target_dir


def _zip_directory(source_dir: Path, zip_path: Path) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            zf.write(file_path, file_path.relative_to(source_dir).as_posix())
    return zip_path


def package_firefox_extension(
    source_dir: Path,
    output_dir: Path,
    *,
    staging_dir: Path | None = None,
    zip_name: str | None = None,
) -> Path:
    """Create a clean Firefox/AMO upload ZIP and return its path."""
    manifest_path = source_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    version = str(manifest.get("version", "0.0.0"))

    stage_root = staging_dir or (output_dir / "_staging" / f"firefox-{version}")
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_firefox_extension(source_dir, stage_root)

    name = zip_name or f"magnetoclip-companion-firefox-{version}.zip"
    zip_path = output_dir / name
    return _zip_directory(stage_root, zip_path)


def package_chromium_extension(
    source_dir: Path,
    output_dir: Path,
    *,
    staging_dir: Path | None = None,
    zip_name: str | None = None,
    key_base64: str | None = None,
    for_store: bool = False,
) -> Path:
    """Create a clean Chromium ZIP and return its path.

    `for_store=True` creates a store-upload package without the manifest `key`
    field (required by Chrome/Edge Add-ons). Otherwise creates a pinned-key
    unpacked/dev package.
    """
    manifest_path = source_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    version = str(manifest.get("version", "0.0.0"))

    default_stage = f"chromium-store-{version}" if for_store else f"chromium-{version}"
    stage_root = staging_dir or (output_dir / "_staging" / default_stage)
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_chromium_extension(
        source_dir, stage_root, key_base64=key_base64, for_store=for_store
    )

    default_name = (
        f"magnetoclip-companion-chromium-store-{version}.zip"
        if for_store
        else f"magnetoclip-companion-chromium-{version}.zip"
    )
    name = zip_name or default_name
    zip_path = output_dir / name
    return _zip_directory(stage_root, zip_path)
