"""License service: activation/validate against the MagnetoClip license server."""

from __future__ import annotations

from .client import (
    BadSignature,
    BoundToOtherPC,
    Expired,
    LicenseClient,
    LicenseError,
    NetworkUnavailable,
    NotBound,
    RateLimited,
    Revoked,
    ServerError,
    UnknownSerial,
)
from .fingerprint import machine_id
from .state import (
    build_client_from_settings,
    clear_serial,
    format_masked_serial,
    last_validated_text,
    mark_validated,
    read_serial,
    store_serial,
)

__all__ = [
    "BadSignature",
    "BoundToOtherPC",
    "Expired",
    "LicenseClient",
    "LicenseError",
    "NetworkUnavailable",
    "NotBound",
    "RateLimited",
    "Revoked",
    "ServerError",
    "UnknownSerial",
    "build_client_from_settings",
    "clear_serial",
    "format_masked_serial",
    "last_validated_text",
    "machine_id",
    "mark_validated",
    "read_serial",
    "store_serial",
]
