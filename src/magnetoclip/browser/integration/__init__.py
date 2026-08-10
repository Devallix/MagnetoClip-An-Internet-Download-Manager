"""Browser extension and native messaging host installation helpers."""

from __future__ import annotations

from .install import (
    build_host_manifest,
    ensure_extension_key,
    extension_id_from_public_key,
    host_manifest_path,
    install,
    uninstall,
    write_host_manifest,
)

__all__ = [
    "build_host_manifest",
    "ensure_extension_key",
    "extension_id_from_public_key",
    "host_manifest_path",
    "install",
    "uninstall",
    "write_host_manifest",
]
