"""Update checker that fetches manifest.json from a remote endpoint."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)


@dataclass
class UpdateInfo:
    """Information about an available update."""

    version: str
    download_url: str
    release_notes: str = ""
    release_date: str = ""
    min_version: str = ""
    size_bytes: int = 0
    filename: str = ""
    sha256: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpdateInfo | None:
        """Create UpdateInfo from manifest.json data."""
        version = data.get("version", "").strip()
        if not version:
            return None
        return cls(
            version=version,
            download_url=data.get("download_url", ""),
            release_notes=data.get("release_notes", ""),
            release_date=data.get("release_date", ""),
            min_version=data.get("min_version", ""),
            size_bytes=data.get("size_bytes", 0),
            filename=data.get("filename", ""),
            sha256=data.get("sha256", ""),
        )


@dataclass
class CheckResult:
    """Result of an update check."""

    update_available: bool
    current_version: str
    latest_version: str
    update_info: UpdateInfo | None = None
    error: str | None = None
    checked_at: str = ""


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string like '0.1.1' into a tuple of integers."""
    parts = version_str.strip().lstrip("v").split(".")
    result = []
    for part in parts:
        try:
            result.append(int(part))
        except ValueError:
            break
    return tuple(result)


class UpdateChecker:
    """Checks for application updates from a remote manifest endpoint."""

    def __init__(self, endpoint: str, timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    async def check(self, current_version: str) -> CheckResult:
        """Check for updates by fetching the manifest.json endpoint.

        The manifest.json format:
        {
            "version": "0.2.0",
            "download_url": "https://releases.magnetoclip.dev/...",
            "release_notes": "...",
            "release_date": "2026-01-15",
            "min_version": "0.1.0",
            "size_bytes": 52428800
        }
        """
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    self.endpoint,
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()

            data = response.json()
            update_info = UpdateInfo.from_dict(data)

            if update_info is None:
                return CheckResult(
                    update_available=False,
                    current_version=current_version,
                    latest_version=current_version,
                    error="Invalid manifest: missing version",
                    checked_at=checked_at,
                )

            current = parse_version(current_version)
            latest = parse_version(update_info.version)

            update_available = latest > current

            return CheckResult(
                update_available=update_available,
                current_version=current_version,
                latest_version=update_info.version,
                update_info=update_info if update_available else None,
                checked_at=checked_at,
            )

        except httpx.TimeoutException:
            log.warning("update_check_timeout", endpoint=self.endpoint)
            return CheckResult(
                update_available=False,
                current_version=current_version,
                latest_version=current_version,
                error="Connection timed out",
                checked_at=checked_at,
            )
        except httpx.HTTPStatusError as exc:
            log.warning("update_check_http_error", status=exc.response.status_code)
            return CheckResult(
                update_available=False,
                current_version=current_version,
                latest_version=current_version,
                error=f"HTTP {exc.response.status_code}",
                checked_at=checked_at,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("update_check_parse_error", exc_info=True)
            return CheckResult(
                update_available=False,
                current_version=current_version,
                latest_version=current_version,
                error="Invalid response format",
                checked_at=checked_at,
            )
        except Exception as exc:
            log.warning("update_check_failed", exc_info=True)
            return CheckResult(
                update_available=False,
                current_version=current_version,
                latest_version=current_version,
                error=str(exc),
                checked_at=checked_at,
            )
