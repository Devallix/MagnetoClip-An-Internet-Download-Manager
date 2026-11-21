"""Browser extension and native messaging host installation helpers."""

from __future__ import annotations

from .install import (
    build_host_manifest,
    ensure_extension_key,
    extension_id_from_public_key,
    get_chromium_extension_public_keys,
    get_primary_chromium_extension_id,
    host_manifest_path,
    install,
    load_pinned_chromium_public_key,
    public_key_base64,
    uninstall,
    write_host_manifest,
)

__all__ = [
    "build_host_manifest",
    "ensure_extension_key",
    "extension_id_from_public_key",
    "get_chromium_extension_public_keys",
    "get_primary_chromium_extension_id",
    "host_manifest_path",
    "install",
    "load_pinned_chromium_public_key",
    "public_key_base64",
    "uninstall",
    "write_host_manifest",
]
