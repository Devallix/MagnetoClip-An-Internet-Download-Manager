"""Update downloader that fetches and installs updates."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from magnetoclip.services.logging.setup import get_logger
from magnetoclip.services.updates.checker import UpdateInfo

log = get_logger(__name__)


@dataclass
class DownloadProgress:
    """Progress information for an update download."""

    bytes_downloaded: int
    total_bytes: int
    percent: float
    speed: float = 0.0

    @property
    def is_complete(self) -> bool:
        return self.total_bytes > 0 and self.bytes_downloaded >= self.total_bytes


class UpdateDownloader:
    """Downloads and installs application updates."""

    def __init__(self) -> None:
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cancellation of the current download."""
        self._cancel_requested = True

    async def download(
        self,
        update_info: UpdateInfo,
        on_progress: Callable[[DownloadProgress], None] | None = None,
    ) -> Path | None:
        """Download the update zip file to a temporary location.

        Returns the path to the downloaded file, or None on failure/cancellation.
        """
        self._cancel_requested = False

        url = update_info.download_url
        if not url:
            log.warning("update_download_no_url")
            return None

        try:
            temp_dir = Path(tempfile.gettempdir()) / "magnetoclip_updates"
            temp_dir.mkdir(parents=True, exist_ok=True)

            filename = self._filename_from_url(url)
            dest = temp_dir / filename

            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()

                    total = int(response.headers.get("content-length", 0))
                    if total == 0 and update_info.size_bytes > 0:
                        total = update_info.size_bytes

                    downloaded = 0
                    with open(dest, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if self._cancel_requested:
                                log.info("update_download_cancelled")
                                return None

                            f.write(chunk)
                            downloaded += len(chunk)

                            if on_progress:
                                on_progress(DownloadProgress(
                                    bytes_downloaded=downloaded,
                                    total_bytes=total,
                                    percent=(downloaded / total * 100) if total > 0 else 0,
                                ))

            if update_info.sha256:
                if not self._verify_sha256(dest, update_info.sha256):
                    log.warning("update_sha256_mismatch")
                    dest.unlink(missing_ok=True)
                    return None

            log.info("update_downloaded", path=str(dest), size=downloaded)
            return dest

        except httpx.HTTPStatusError as exc:
            log.warning("update_download_http_error", status=exc.response.status_code)
            return None
        except httpx.TimeoutException:
            log.warning("update_download_timeout")
            return None
        except Exception as exc:
            log.warning("update_download_failed", exc_info=True)
            return None

    def install(self, zip_path: Path) -> bool:
        """Extract the zip file and replace the current installation.

        The zip should contain MagnetoClip.exe and _internal/ at the root.
        Returns True if the installation was successful.
        """
        if not zip_path.is_file():
            log.warning("update_install_file_not_found", path=str(zip_path))
            return False

        try:
            install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path.cwd()
            log.info("update_install_dir", path=str(install_dir))

            backup_dir = install_dir.parent / "MagnetoClip_backup"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)

            install_dir.rename(backup_dir)
            log.info("update_backup_created", path=str(backup_dir))

            install_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(install_dir)
                log.info("update_extracted", path=str(install_dir))

            self._cleanup_backup(backup_dir)

            log.info("update_installed", path=str(install_dir))
            return True

        except Exception as exc:
            log.warning("update_install_failed", exc_info=True)
            self._restore_backup(backup_dir, install_dir)
            return False

    def _verify_sha256(self, file_path: Path, expected_hash: str) -> bool:
        """Verify the SHA256 hash of a file."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        actual_hash = sha256.hexdigest()
        return actual_hash.lower() == expected_hash.lower()

    def _cleanup_backup(self, backup_dir: Path) -> None:
        """Remove backup directory after successful update."""
        try:
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
                log.info("update_backup_removed", path=str(backup_dir))
        except Exception:
            log.warning("update_backup_cleanup_failed", exc_info=True)

    def _restore_backup(self, backup_dir: Path, install_dir: Path) -> None:
        """Restore from backup if update failed."""
        try:
            if install_dir.exists():
                shutil.rmtree(install_dir)
            if backup_dir.exists():
                backup_dir.rename(install_dir)
                log.info("update_backup_restored", path=str(install_dir))
        except Exception:
            log.warning("update_backup_restore_failed", exc_info=True)

    @staticmethod
    def _filename_from_url(url: str) -> str:
        """Extract a reasonable filename from a download URL."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = Path(parsed.path)
        name = path.name

        if not name or "." not in name:
            name = "MagnetoClip-Update.zip"

        return name
