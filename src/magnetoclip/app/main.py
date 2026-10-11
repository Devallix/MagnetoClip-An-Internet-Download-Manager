from __future__ import annotations
import asyncio
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from magnetoclip.core.events.bus import Events

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MagnetoClip")
    except Exception:
        pass

import qasync
from PySide6.QtCore import QEventLoop
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
_IPC_SERVER_NAME = "magnetoclip-ipc"

# Floor on how long the splash stays up, so it is readable rather than a flash.
# Startup work that runs longer than this keeps the splash up for its real
# duration; nothing is ever added on top.
SPLASH_MIN_MS = 900


def _extract_magnet_uri(argv: list[str]) -> str | None:
    """Return the first magnet: URI found in *argv*, or ``None``."""
    for arg in argv[1:]:
        if arg.lower().startswith("magnet:?") and not arg.startswith("-"):
            return arg
    return None


def _extract_command_uri(argv: list[str]) -> str | None:
    """Return the first ``magneto://`` command URI found in *argv*, or ``None``."""
    for arg in argv[1:]:
        if arg.lower().startswith("magneto://") and not arg.startswith("-"):
            return arg
    return None


def _pump_until(app, predicate, timeout: float = 120.0) -> None:
    """Run the Qt event loop until *predicate* is true, or *timeout* elapses.

    Keeps timers, repaints and input alive while worker-thread startup work is
    in flight, which is what lets the splash actually animate.
    """
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)
    app.processEvents(QEventLoop.AllEvents, 20)


def _build_context_animated(app):
    """Build the app context on a worker thread while the UI stays responsive.

    ``build_context`` only touches paths, logging, SQLite, settings and the
    event bus -- no Qt widgets -- so it is safe off the UI thread. Doing it in
    the foreground froze the splash for over a second with no visible motion.

    The event bus is created here on the UI thread and handed into
    ``build_context``: a QObject produced inside the helper thread would be
    affiliated to that thread's own (unpumped) Qt event loop, silently dropping
    every UI event posted later (navigation, notifications, download updates).
    """
    import threading

    from magnetoclip.core.events.bus import EventBus

    events = EventBus()
    result: dict[str, object] = {}

    def _work() -> None:
        try:
            result["context"] = build_context(events=events)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            result["error"] = exc

    thread = threading.Thread(target=_work, name="magnetoclip-startup", daemon=True)
    thread.start()
    _pump_until(app, lambda: not thread.is_alive())
    thread.join()
    error = result.get("error")
    if error is not None:
        raise error
    return result["context"]


def _dismiss_splash(app, splash) -> None:
    """Fade the splash out and let the fade finish before returning.

    Needed on the early-exit paths, where the fade animation has to be given
    event-loop time or the splash would be left stranded on screen.
    """
    splash.close_with_fade()
    deadline = time.monotonic() + 0.5
    while splash.isVisible() and time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


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

    # Show the splash before any startup work so the wait is covered by
    # something moving. Everything below either runs on a worker thread or is
    # quick enough not to matter.
    splash = SplashScreen()
    splash.show()
    splash_shown_at = time.monotonic()

    context = _build_context_animated(app)
    log.info("application_starting", version=__version__)

    if context.settings.get("torrent.file_association", False):
        from magnetoclip.torrent.file_association import is_registered, register
        if not is_registered():
            register()

    if context.settings.get("torrent.magnet_protocol", False):
        from magnetoclip.torrent.file_association import (
            is_magnet_registered,
            register_magnet,
        )
        if not is_magnet_registered():
            register_magnet()

    from magnetoclip.services.shell.protocol import (
        is_magneto_registered,
        register_magneto_protocol,
    )
    if not is_magneto_registered():
        register_magneto_protocol()

    lock = acquire_single_instance_lock(context.data_dir)
    if lock is None:
        magnet_uri = _extract_magnet_uri(argv)
        torrent_file = _find_torrent_arg(argv)
        command_uri = _extract_command_uri(argv)
        payload = magnet_uri or torrent_file or command_uri
        _dismiss_splash(app, splash)
        if payload and _forward_to_running_instance(payload):
            log.info("forwarded_to_running_instance", payload=payload[:80])
            return 0
        QMessageBox.information(None, "MagnetoClip", "MagnetoClip is already running.")
        return 1

    # License gate: verify online before anything else runs (v1 policy).
    # A 7-day trial is granted on first launch; the gate is skipped while
    # the trial is active.
    from magnetoclip.services.licensing.trial import ensure_trial_started
    from magnetoclip.ui.dialogs.activation import run_activation_gate

    ensure_trial_started(context.settings, context.session_factory)
    if not run_activation_gate(context):
        log.info("license_gate_aborted")
        _dismiss_splash(app, splash)
        lock.unlock()
        return 1

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    # MainWindow is built here rather than behind the splash deliberately: it
    # touches widgets, so it has to stay on the UI thread. It is only a few
    # hundred milliseconds now that pages are built lazily.
    window = MainWindow(context)

    # Listen for forwarded magnet/.torrent payloads right away so clicks
    # that arrive while the splash screen is still up are not lost.
    _start_ipc_server(window)

    torrent_file_arg = _find_torrent_arg(argv)
    magnet_uri_arg = _extract_magnet_uri(argv)
    command_uri_arg = _extract_command_uri(argv)

    if context.settings.get("browser.integration_enabled", False):
        try:
            context.browser.ensure_installed()
        except Exception:  # noqa: BLE001 - integration must not prevent startup
            log.warning("browser_integration_ensure_failed", exc_info=True)

    async def _run_until_close() -> None:
        import asyncio as _asyncio

        # Wait out only the remainder of a short minimum. The old fixed sleep
        # added several seconds of dead time even when startup had already
        # finished, and the heavy work it was meant to cover had already
        # finished before the splash was even shown.
        remaining = SPLASH_MIN_MS / 1000.0 - (time.monotonic() - splash_shown_at)
        if remaining > 0:
            await _asyncio.sleep(remaining)

        splash.close_with_fade()
        window.show()
        log.info("main_window_shown")

        # Start background loops for the pause auto-resume timer and auto speed
        # tests now that the event loop is actually running (they cannot be
        # created earlier in build_context because no loop exists yet).
        pauses = getattr(context, "pauses", None)
        if pauses is not None:
            pauses.start_loop()
        speedtest = getattr(context, "speedtest", None)
        if speedtest is not None:
            speedtest.start_auto()

        # Bring up the LAN dashboard while the window task is safely
        # suspended: creating tasks during a synchronous stretch of this
        # coroutine risks qasync stepping them re-entrantly (Py3.13 guard).
        from magnetoclip.services.remote import ensure_server

        asyncio.ensure_future(ensure_server(context))

        if magnet_uri_arg:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, lambda: _open_torrent_file(window, magnet_uri_arg))

        if torrent_file_arg:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, lambda: _open_torrent_file(window, torrent_file_arg))

        if command_uri_arg:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, lambda uri=command_uri_arg: _dispatch_command_uri(window, uri))

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


def _find_torrent_arg(argv: list[str]) -> str | None:
    """Return the first .torrent file path found in *argv*, or ``None``."""
    for arg in argv[1:]:
        if arg.lower().endswith(".torrent") and not arg.startswith("-"):
            return arg
    return None


def _forward_to_running_instance(payload: str) -> bool:
    """Try to send *payload* (magnet URI or .torrent path) to a running instance.

    Returns ``True`` if the message was delivered.
    """
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(_IPC_SERVER_NAME)
    if not socket.waitForConnected(2000):
        return False
    data = payload.encode("utf-8")
    socket.write(data)
    socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()
    return True


def _start_ipc_server(window) -> None:
    """Create a QLocalServer that receives forwarded URIs from duplicate instances."""
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer(window)
    # Remove any stale server from a previous crash
    QLocalServer.removeServer(_IPC_SERVER_NAME)
    if not server.listen(_IPC_SERVER_NAME):
        log.warning("ipc_server_listen_failed", error=server.errorString())
        return

    def _on_new_connection() -> None:
        while socket := server.nextPendingConnection():
            if socket.waitForReadyRead(3000):
                raw = socket.readAll().data().decode("utf-8", errors="replace")
                if raw:
                    log.info("ipc_received", payload=raw[:80])
                    from PySide6.QtCore import QTimer
                    if raw.lower().startswith("magneto://"):
                        QTimer.singleShot(100, lambda uri=raw: _dispatch_command_uri(window, uri))
                    else:
                        QTimer.singleShot(100, lambda url=raw: _open_torrent_file(window, url))
            socket.deleteLater()

    server.newConnection.connect(_on_new_connection)
    # prevent GC
    window._ipc_server = server


def _dispatch_command_uri(window, uri: str) -> None:
    """Apply a ``magneto://`` command delivered via argv or IPC on the GUI window."""
    from magnetoclip.services.shell.commands import apply_command

    try:
        apply_command(window, uri)
    except Exception as exc:
        log.warning("command_uri_failed", error=str(exc), uri=uri[:80])


def _open_torrent_file(window, torrent_path: str) -> None:
    """Open the AddTorrentDialog for a .torrent file or magnet URI."""
    from magnetoclip.torrent.detect import is_magnet_link
    from magnetoclip.ui.dialogs.add_torrent import AddTorrentDialog

    try:
        window.show()
        window.raise_()
        window.activateWindow()
        window._activate_nav("torrents")
        dialog = AddTorrentDialog(window.context, parent=window, url=torrent_path)
        if dialog.exec():
            manager = window.context.manager
            try:
                kwargs: dict = {"url": dialog.url()}
                if dialog.filename():
                    kwargs["filename"] = dialog.filename()
                if dialog.category():
                    kwargs["category_name"] = dialog.category()
                download = manager.add(**kwargs)
                if is_magnet_link(download.url) or download.detected_type == "torrent":
                    manager._pending_torrent_opts[download.id] = {
                        "sequential": dialog.sequential(),
                        "seed_mode": dialog.seed_mode(),
                    }
            except Exception as exc:
                log.warning("torrent_file_open_failed", error=str(exc))
                return
            manager.torrent_queue.admit_and_advance()
    except Exception as exc:
        log.warning("torrent_file_open_failed", error=str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
