from __future__ import annotations

from magnetoclip.database.repositories import SpeedTestRepository
from magnetoclip.database.session import Database
from magnetoclip.services.speedtest.analyzer import SpeedAnalyzer


def _make_context(tmp_path):
    class Settings:
        def __init__(self):
            self._d = {"speedtest.throttle_sensitivity": 0.3}

        def get(self, key, default=None):
            return self._d.get(key, default)

    db = Database(tmp_path / "s.db")
    db.initialize()

    class Ctx:
        session_factory = db.Session

    ctx = Ctx()
    ctx.settings = Settings()
    ctx.db = db
    return ctx


def test_stats_and_history(tmp_path):
    ctx = _make_context(tmp_path)
    with ctx.db.Session() as s:
        repo = SpeedTestRepository(s)
        repo.add(download_mbps=55.5, upload_mbps=12.0, latency_ms=18.0, server_name="A")
        repo.add(download_mbps=80.0, upload_mbps=14.0, latency_ms=15.0, server_name="B")

    analyzer = SpeedAnalyzer(ctx)
    stats = analyzer.stats()
    assert stats["count"] == 2
    assert abs(stats["avg_download_mbps"] - 67.75) < 0.01

    history = analyzer.history(limit=5)
    assert len(history) == 2
    speeds = {h["download_mbps"] for h in history}
    assert speeds == {55.5, 80.0}
    ctx.db.close()


def test_analyzer_add_result_and_prune(tmp_path):
    ctx = _make_context(tmp_path)
    analyzer = SpeedAnalyzer(ctx)
    analyzer.add_result(
        {"download_mbps": 30.0, "upload_mbps": 10.0, "latency_ms": 20.0, "server_name": "X"}
    )
    assert analyzer.stats()["count"] == 1
    assert analyzer.prune(keep=0) == 1
    assert analyzer.stats()["count"] == 0
    ctx.db.close()


def test_throttle_analysis_insufficient_data(tmp_path):
    ctx = _make_context(tmp_path)
    analyzer = SpeedAnalyzer(ctx)
    res = analyzer.throttle_analysis()
    assert res["flagged"] is False
    assert res["reason"] == "Not enough data"
    ctx.db.close()