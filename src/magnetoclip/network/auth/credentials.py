from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import keyring

from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)

SERVICE_NAME = "MagnetoClip"


@dataclass
class AuthSpec:
    """Authentication for a download.

    ``type`` is one of ``none/basic/bearer``.
    """

    type: str = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def to_httpx_auth(self) -> tuple[str, str] | None:
        if self.type == "basic" and self.username is not None:
            return (self.username, self.password or "")
        return None

    def headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if self.type == "bearer" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "username": self.username,
            "password": self.password,
            "token": self.token,
            "extra_headers": self.extra_headers,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuthSpec:
        data = data or {}
        return cls(
            type=data.get("type", "none"),
            username=data.get("username"),
            password=data.get("password"),
            token=data.get("token"),
            extra_headers=data.get("extra_headers", {}),
        )


def set_secret(username: str, password: str) -> None:
    """Store a credential in the platform secure store."""
    try:
        keyring.set_password(SERVICE_NAME, username, password)
    except Exception:  # pragma: no cover - backend dependent
        log.warning("keyring_unavailable_for_set", username=username)


def get_secret(username: str) -> str | None:
    """Read a credential from the platform secure store."""
    try:
        return keyring.get_password(SERVICE_NAME, username)
    except Exception:  # pragma: no cover - backend dependent
        log.warning("keyring_unavailable_for_get", username=username)
        return None


def delete_secret(username: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, username)
    except Exception:  # pragma: no cover - backend dependent
        log.warning("keyring_unavailable_for_delete", username=username)
