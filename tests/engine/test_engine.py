import asyncio
import hashlib

import pytest

from magnetoclip.engine.downloader.engine import (
    DownloadTask,
    MagnetoCore,
    analyze,
    spec_from_url,
)
from magnetoclip.engine.resume.mclip import MClipState, SegmentState

from tests.support.http_server import PayloadServer, html_server, no_range_server

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB deterministic


def _make_spec(server, tmp_path, **overrides):
    defaults = dict(
        download_id=1,
        url=server.url,
        save_dir=tmp_path,
        filename="out.bin",
        connections_max=4,
        retry_max=3,
        retry_base=0.01,
    )
    defaults.update(overrides)
    return spec_from_url(**defaults)


@pytest.mark.asyncio
async def test_analyze_detects_size_and_ranges():
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            info = await analyze(core._client, server.url)
            assert info.total_size == len(PAYLOAD)
            assert info.supports_ranges is True
            assert info.etag == '"test-etag"'
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_full_download_matches_payload(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path)
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            data = spec.final_path.read_bytes()
            assert data == PAYLOAD
            assert not spec.final_path.with_name(spec.filename + ".mclip").exists()
            assert server.range_requests > 0
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_download_with_hash_verification(tmp_path):
    expected = hashlib.sha256(PAYLOAD).hexdigest()
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(
                server, tmp_path, hash_algo="sha256", hash_expected=expected
            )
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_download_hash_mismatch_fails(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(
                server,
                tmp_path,
                hash_algo="sha256",
                hash_expected="0" * 64,
            )
            task = core.submit(spec)
            result = await task.run()
            assert result == "verification_failed"
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_resume_from_partial_state(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=2)
            # pre-download first 300KB into segment 0
            state = MClipState(
                url=spec.url,
                file_path=str(spec.final_path),
                total_size=len(PAYLOAD),
                headers={},
            )
            state.segments = [
                SegmentState(index=0, start=0, end=len(PAYLOAD) // 2 - 1, written=0),
                SegmentState(index=1, start=len(PAYLOAD) // 2, end=len(PAYLOAD) - 1, written=0),
            ]
            state.part_path(0).write_bytes(PAYLOAD[: 300 * 1024])
            state.segments[0].written = 300 * 1024

            task = core.submit(spec, state)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_resume_extends_plan_when_file_grew(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            # Simulate a previous run that planned for a smaller file (the
            # server now serves the full PAYLOAD). The tail must be fetched
            # instead of merging a truncated file.
            half = len(PAYLOAD) // 2
            state = MClipState(
                url=spec.url,
                file_path=str(spec.final_path),
                total_size=half,
                headers={},
            )
            state.segments = [
                SegmentState(index=0, start=0, end=half - 1, written=half),
            ]
            state.part_path(0).write_bytes(PAYLOAD[:half])

            task = core.submit(spec, state)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_resume_trims_plan_when_file_shrank(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            # Simulate a previous run that planned for a larger file; the
            # server now serves PAYLOAD. The plan must shrink to the new EOF.
            state = MClipState(
                url=spec.url,
                file_path=str(spec.final_path),
                total_size=len(PAYLOAD) * 2,
                headers={},
            )
            state.segments = [
                SegmentState(index=0, start=0, end=len(PAYLOAD) * 2 - 1, written=len(PAYLOAD)),
            ]
            state.part_path(0).write_bytes(PAYLOAD)

            task = core.submit(spec, state)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_pause_resume(tmp_path):
    slow = bytes(range(256)) * 512  # 128 KiB, slow stream
    with PayloadServer(slow, chunk_size=4096, chunk_delay=0.02) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            task = core.submit(spec)
            runner = asyncio.create_task(task.run())
            await asyncio.sleep(0.3)
            task.pause()
            assert task.state.state == "paused"
            await asyncio.sleep(0.1)
            assert not task.done.is_set()
            task.resume()
            await runner
            assert spec.final_path.read_bytes() == slow
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_pause_during_connecting_then_resume(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            task = core.submit(spec)
            original = task._analyze_with_retry

            async def analyze_then_pause():
                info = await original()
                task.pause()
                return info

            task._analyze_with_retry = analyze_then_pause
            runner = asyncio.create_task(task.run())

            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if task.state.state == "paused" and not task._running.is_set():
                    break
                await asyncio.sleep(0.02)
            # A pause during "connecting" must survive the analysis phase: the
            # state that follows must still read "paused" so resume() works
            # instead of leaving the download stuck.
            assert task.state.state == "paused"
            assert not task._running.is_set()

            task.resume()
            await runner
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_cancel(tmp_path):
    slow = bytes(range(256)) * 1024  # 256 KiB, slow stream
    with PayloadServer(slow, chunk_size=4096, chunk_delay=0.02) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            task = core.submit(spec)
            runner = asyncio.create_task(task.run())
            await asyncio.sleep(0.3)
            task.cancel()
            result = await runner
            assert result == "stopped"
            assert not spec.final_path.exists()
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_range_not_supported_falls_back(tmp_path):
    with no_range_server(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=4)
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_transient_failures_are_retried(tmp_path):
    with PayloadServer(PAYLOAD, fail_times=2) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_retry_max_zero_still_downloads(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, retry_max=0)
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_retry_max_zero_single_attempt_then_fails(tmp_path):
    with PayloadServer(PAYLOAD, fail_times=1) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, retry_max=0)
            task = core.submit(spec)
            result = await task.run()
            assert result == "failed"
            assert server.requests == 1
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_permanent_http_error_fails(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            spec.url = server.base + "/missing.bin"
            task = core.submit(spec)
            result = await task.run()
            assert result == "failed"
            assert task.error
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_html_error_page_does_not_override_binary(tmp_path):
    html = b"<html><head><title>Not Found</title></head><body>404</body></html>"
    with html_server(html) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, filename="out.bin")
            task = core.submit(spec)
            result = await task.run()
            assert result == "failed"
            assert "HTML" in task.error
            assert not spec.final_path.exists()
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_html_content_saved_when_filename_is_html(tmp_path):
    html = b"<html><body>hello</body></html>"
    with html_server(html) as server:
        core = MagnetoCore()
        try:
            spec = _make_spec(server, tmp_path, filename="page.html")
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == html
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_bandwidth_limiting_slows_download(tmp_path):
    payload = bytes(range(256)) * 512  # 128 KiB
    with PayloadServer(payload) as server:
        core = MagnetoCore(
            bandwidth_bytes_per_second=64 * 1024,
            bandwidth_capacity=64 * 1024,  # no burst: sustained limit
        )
        try:
            spec = _make_spec(server, tmp_path, connections_max=1)
            task = core.submit(spec)
            start = asyncio.get_event_loop().time()
            await task.run()
            elapsed = asyncio.get_event_loop().time() - start
            assert elapsed >= 1.0  # 128KiB @ 64KiB/s = 2s
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_priority_allocates_per_task_limiters(tmp_path):
    core = MagnetoCore(
        bandwidth_bytes_per_second=300.0,
        bandwidth_capacity=300.0,
    )
    try:
        tasks = {}
        for download_id, priority in ((1, 0), (2, 2), (3, 0)):
            spec = spec_from_url(
                download_id=download_id,
                url="http://127.0.0.1:1/unused",
                save_dir=tmp_path,
                filename=f"f{download_id}.bin",
                connections_max=1,
                priority=priority,
            )
            tasks[download_id] = core.submit(spec)
        # Low priority (weight 1) gets half the share of high priority (weight 3).
        low = tasks[1].limiter.rate
        high = tasks[2].limiter.rate
        assert high == low * 3
        # Two equal-priority tasks share the budget evenly.
        assert tasks[3].limiter.rate == pytest.approx(low)
        assert low + high + tasks[3].limiter.rate == pytest.approx(300.0)
        # Removing a task re-shares the budget among the survivors.
        core.remove_task(2)
        assert tasks[1].limiter.rate == pytest.approx(150.0)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_set_priority_reallocates_bandwidth(tmp_path):
    core = MagnetoCore(
        bandwidth_bytes_per_second=600.0,
        bandwidth_capacity=600.0,
    )
    try:
        tasks = {}
        for download_id in (1, 2):
            spec = spec_from_url(
                download_id=download_id,
                url="http://127.0.0.1:1/unused",
                save_dir=tmp_path,
                filename=f"f{download_id}.bin",
                connections_max=1,
            )
            tasks[download_id] = core.submit(spec)
        assert tasks[1].limiter.rate == pytest.approx(300.0)
        assert tasks[2].limiter.rate == pytest.approx(300.0)
        core.set_priority(1, 3)  # weight 4 vs 1
        assert tasks[1].limiter.rate == pytest.approx(480.0)
        assert tasks[2].limiter.rate == pytest.approx(120.0)
    finally:
        await core.shutdown()
