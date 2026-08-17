from __future__ import annotations
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MagnetoClip")
    except Exception:
        pass

import qasync
from PySide6.QtWidgets import QApplication, QMessageBox

from magnetoclip.app.lifecycle import acquire_single_instance_lock, build_context
from magnetoclip.browser.manager import BrowserManager
from magnetoclip.browser.native_messaging.host import run_host
from magnetoclip.resources import app_icon
from magnetoclip.services.logging.setup import get_logger
from magnetoclip.ui.main_window import MainWindow
from magnetoclip.ui.splash import SplashScreen
from magnetoclip.version import __version__

log = get_logger(__name__)

BROWSER_HOST_FLAG = "--browser-host"


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if BROWSER_HOST_FLAG in argv:
        return _browser_host_main(argv)

    app = QApplication(argv)
    app.setApplicationName("MagnetoClip")
    app.setOrganizationName("MagnetoClip")
    app.setApplicationDisplayName(f"MagnetoClip {__version__}")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(True)

    context = build_context()
    log.info("application_starting", version=__version__)

    lock = acquire_single_instance_lock(context.data_dir)
    if lock is None:
        QMessageBox.information(None, "MagnetoClip", "MagnetoClip is already running.")
        return 1

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow(context)

    if context.settings.get("browser.integration_enabled", False):
        try:
            context.browser.ensure_installed()
        except Exception:  # noqa: BLE001 - integration must not prevent startup
            log.warning("browser_integration_ensure_failed", exc_info=True)

    async def _run_until_close() -> None:
        splash = SplashScreen()
        splash.show()
        app.processEvents()

        import asyncio as _asyncio
        await _asyncio.sleep(4)

        if context.settings.get("scheduler.enabled", False):
            try:
                await context.scheduler.start()
            except Exception:  # noqa: BLE001 - scheduler must not prevent startup
                log.warning("scheduler_start_failed", exc_info=True)

        splash.close_with_fade()
        window.show()
        log.info("main_window_shown")

        if context.settings.get("updates.check_enabled", True):
            from PySide6.QtCore import QThread, Signal

            class _BackgroundUpdateCheck(QThread):
                """Background thread for startup update check."""

                finished = Signal(object)

                def __init__(self) -> None:
                    super().__init__()

                def run(self) -> None:
                    from magnetoclip.services.updates import UpdateChecker

                    endpoint = context.settings.get("updates.endpoint", "")
                    if not endpoint:
                        self.finished.emit(None)
                        return
                    checker = UpdateChecker(endpoint)
                    result = checker.check_sync(__version__)
                    self.finished.emit(result)

            def _on_background_check_done(result) -> None:
                if result is None:
                    return
                from datetime import UTC, datetime

                now = datetime.now(UTC).isoformat(timespec="seconds")
                context.settings.set("updates.last_checked", now)

                if result.update_available:
                    log.info(
                        "update_available",
                        current=__version__,
                        latest=result.latest_version,
                    )
                    context.events.post(Events.UPDATE_AVAILABLE, {
                        "current_version": __version__,
                        "latest_version": result.latest_version,
                        "update_info": result.update_info,
                    })
                else:
                    log.info("no_update_available", version=__version__)

            _bg_worker = _BackgroundUpdateCheck()
            _bg_worker.finished.connect(_on_background_check_done)
            _bg_worker.start()
            # prevent garbage collection
            context._bg_update_worker = _bg_worker

        try:
            while window.isVisible():
                await asyncio.sleep(0.05)
        finally:
            await context.shutdown()
            loop.stop()

    with loop:
        loop.create_task(_run_until_close())
        loop.run_forever()

    lock.unlock()
    log.info("application_stopped")
    return 0


def _browser_host_main(argv: Sequence[str]) -> int:
    """Run as a native messaging host: read from stdin, respond on stdout."""
    context = build_context()
    log.info("browser_host_started")

    async def _host_main() -> None:
        loop = asyncio.get_running_loop()
        bridge = BrowserManager(context)
        bridge.start(loop)
        await asyncio.to_thread(
            run_host,
            bridge.handle_message,
            outbound=bridge.next_outbound_message,
        )
        log.info("browser_host_finished")
        await context.shutdown()
        loop.stop()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.create_task(_host_main())
        loop.run_forever()
    finally:
        loop.close()
    log.info("browser_host_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
