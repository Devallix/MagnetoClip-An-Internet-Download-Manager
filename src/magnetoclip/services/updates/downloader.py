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

    def download_sync(
        self,
        update_info: UpdateInfo,
        on_progress: Callable[[DownloadProgress], None] | None = None,
    ) -> Path | None:
        """Synchronous download of the update zip file to a temporary location.

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

            with httpx.Client(timeout=600, follow_redirects=True) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()

                    total = int(response.headers.get("content-length", 0))
                    if total == 0 and update_info.size_bytes > 0:
                        total = update_info.size_bytes

                    downloaded = 0
                    with open(dest, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=65536):
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

        On Windows the running executable and DLLs are locked by the OS, so we
        cannot rename the install directory directly.  Instead we:

        1. Extract the zip into a temporary staging directory.
        2. Write a small ``.bat`` helper that waits for the app to exit, swaps
           the directories, cleans up, and optionally restarts.
        3. Launch the helper and return – the caller should quit the app.

        On other platforms we do the swap in-process.

        The zip should contain ``MagnetoClip.exe`` and ``_internal/`` at the
        root level.
        """
        if not zip_path.is_file():
            log.warning("update_install_file_not_found", path=str(zip_path))
            return False

        try:
            install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path.cwd()
            log.info("update_install_dir", path=str(install_dir))

            staging_dir = Path(tempfile.mkdtemp(prefix="magnetoclip_update_"))
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(staging_dir)
            log.info("update_staging_extracted", path=str(staging_dir))

            if sys.platform == "win32":
                return self._launch_swap_batch(install_dir, staging_dir)
            else:
                return self._swap_direct(install_dir, staging_dir)

        except Exception as exc:
            log.warning("update_install_failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Windows – out-of-process swap via a helper batch script
    # ------------------------------------------------------------------

    def _launch_swap_batch(self, install_dir: Path, staging_dir: Path) -> bool:
        """Write and launch a ``.bat`` that swaps old ↔ new files after the app exits."""
        import subprocess

        # The batch must NOT live inside staging_dir: cmd keeps an open handle
        # to the executing .bat, so a later ``rmdir`` of staging would fail and
        # leave garbage behind. A separate temp dir also lets xcopy mirror the
        # staged update 1:1.
        bat_dir = Path(tempfile.mkdtemp(prefix="magnetoclip_swap_"))
        bat_path = bat_dir / "_swap.bat"
        exe_name = "MagnetoClip.exe"
        log_file = bat_dir / "update.log"
        # Identify *this* process specifically. Waiting on the image name is
        # not enough: the browser-integration native host runs the very same
        # MagnetoClip.exe, so a name-based loop spins forever while any host
        # instance lives.
        main_pid = os.getpid()

        # The batch script:
        #   1. Waits until the main app process (by PID) exits — at most ~90 s,
        #      then force-kills that PID so the swap can never hang forever.
        #   2. Force-stops leftover helper instances (browser-host), which
        #      would otherwise keep the executable locked.
        #   3. Removes old _internal/ and the old exe, retrying briefly —
        #     antivirus/indexer handles can hold locks for a few seconds.
        #   4. Copies fresh files from staging into the install dir, verifying
        #      xcopy succeeded before continuing.
        #   5. Cleans up the staging directory and restarts.
        # ``ping`` is used as the sleep because ``timeout`` misbehaves without
        # an interactive console; each ping -n 2 waits roughly one second.
        bat_content = (
            "@echo off\r\n"
            "chcp 65001 >NUL\r\n"
            f'echo [%DATE% %TIME%] update swap started >"{log_file}"\r\n'
            "\r\n"
            "REM --- wait for the main MagnetoClip process to exit (max ~90s) ---\r\n"
            "set /a tries=0\r\n"
            ":wait_loop\r\n"
            f'tasklist /FI "PID eq {main_pid}" 2>NUL | find /I "{main_pid}" >NUL\r\n'
            "if %ERRORLEVEL% NEQ 0 goto wait_done\r\n"
            "set /a tries+=1\r\n"
            f"if %tries% GEQ 90 goto force_kill\r\n"
            "ping -n 2 127.0.0.1 >NUL\r\n"
            "goto wait_loop\r\n"
            ":force_kill\r\n"
            f'echo main PID {main_pid} still alive after 90s - force killing >>"{log_file}"\r\n'
            f"taskkill /f /pid {main_pid} >NUL 2>&1\r\n"
            "ping -n 3 127.0.0.1 >NUL\r\n"
            ":wait_done\r\n"
            "\r\n"
            "REM --- stop leftover helper instances (browser-host) ---\r\n"
            f'taskkill /f /im {exe_name} >NUL 2>&1\r\n'
            "ping -n 2 127.0.0.1 >NUL\r\n"
            "\r\n"
            "REM --- remove old files, then copy new ones (with retries) ---\r\n"
            "set /a copy_tries=0\r\n"
            ":swap_loop\r\n"
            f'rmdir /s /q "{install_dir}\\_internal" >NUL 2>&1\r\n'
            f'del /f /q "{install_dir}\\{exe_name}" >NUL 2>&1\r\n'
            f'xcopy /i /s /e /y /q "{staging_dir}\\*" "{install_dir}\\" >NUL 2>&1\r\n'
            "if %ERRORLEVEL% EQU 0 goto swap_ok\r\n"
            "set /a copy_tries+=1\r\n"
            f'echo swap attempt %copy_tries% failed >>"{log_file}"\r\n'
            "if %copy_tries% GEQ 10 goto swap_fail\r\n"
            "ping -n 2 127.0.0.1 >NUL\r\n"
            "goto swap_loop\r\n"
            ":swap_fail\r\n"
            f'echo FATAL: swap failed after 10 attempts >>"{log_file}"\r\n'
            "exit /b 1\r\n"
            ":swap_ok\r\n"
            f'echo swap completed >>"{log_file}"\r\n'
            "\r\n"
            "REM --- cleanup ---\r\n"
            f'rmdir /s /q "{staging_dir}" >NUL 2>&1\r\n'
            f'del /f /q "{log_file}" >NUL 2>&1\r\n'
            "\r\n"
            "REM --- restart ---\r\n"
            'start "" "' + str(install_dir / exe_name) + '"\r\n'
        )

        try:
            bat_path.write_text(bat_content, encoding="utf-8")
        except UnicodeEncodeError:
            bat_path.write_text(bat_content, encoding="ascii", errors="replace")
        log.info("update_batch_created", path=str(bat_path))

        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log.info("update_batch_launched")
        return True

    # ------------------------------------------------------------------
    # Unix – in-process swap (files are not locked)
    # ------------------------------------------------------------------

    def _swap_direct(self, install_dir: Path, staging_dir: Path) -> bool:
        """Swap directories in-process (safe on Linux / macOS)."""
        backup_dir = install_dir.parent / "MagnetoClip_backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)

        install_dir.rename(backup_dir)
        log.info("update_backup_created", path=str(backup_dir))

        shutil.copytree(staging_dir, install_dir)
        log.info("update_files_copied", path=str(install_dir))

        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)

        log.info("update_installed", path=str(install_dir))
        return True

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
