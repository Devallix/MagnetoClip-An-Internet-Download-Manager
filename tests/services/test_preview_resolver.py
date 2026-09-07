from __future__ import annotations

from magnetoclip.services.preview.resolver import PreviewResolver, PreviewType


class _FakeSettings:
    def get(self, key, default=None):
        assert key == "preview.enabled"
        return default


class _Ctx:
    settings = _FakeSettings()


def test_extension_mapping(tmp_path):
    resolver = PreviewResolver(_Ctx())
    cases = {
        "photo.JPG": PreviewType.IMAGE,
        "notes.txt": PreviewType.TEXT,
        "code.py": PreviewType.TEXT,
        "video.mp4": PreviewType.VIDEO,
        "audio.mp3": PreviewType.AUDIO,
        "doc.pdf": PreviewType.PDF,
        "ubuntu.torrent": PreviewType.TORRENT,
    }
    for name, expected in cases.items():
        p = tmp_path / name
        p.write_bytes(b"x")
        fmt = resolver.resolve(p)
        assert fmt == expected, f"{name}: got {fmt}"
    assert resolver.resolve(tmp_path / "unknown.zzz") == PreviewType.NONE


def test_missing_file_is_none(tmp_path):
    resolver = PreviewResolver(_Ctx())
    assert resolver.resolve(tmp_path / "does_not_exist.pdf") == PreviewType.NONE
    assert resolver.resolve(None) == PreviewType.NONE


def test_text_too_large_is_none(tmp_path):
    resolver = PreviewResolver(_Ctx())
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    assert resolver.resolve(big) == PreviewType.NONE


def test_preview_types():
    assert set(PreviewType) == {
        PreviewType.IMAGE,
        PreviewType.VIDEO,
        PreviewType.AUDIO,
        PreviewType.PDF,
        PreviewType.TORRENT,
        PreviewType.TEXT,
        PreviewType.NONE,
    }


class _FakeDownload:
    def __init__(self, save_path, detected_type="torrent", url=None):
        self.save_path = save_path
        self.detected_type = detected_type
        self.url = url
        self.media_metadata_json = None


def test_torrent_candidates_fallback(tmp_path):
    resolver = PreviewResolver(_Ctx())
    (tmp_path / "movie.torrent").write_bytes(b"d8:announce0e")
    (tmp_path / ".torrent_abcdef3210.tmp").write_bytes(b"d8:announce0e")
    download = _FakeDownload(str(tmp_path / "missing.torrent"))
    candidates = resolver.filenames(download)
    assert str(tmp_path / "movie.torrent") in candidates
    assert str(tmp_path / ".torrent_abcdef3210.tmp") in candidates
    assert resolver.type_of_download(download) == PreviewType.TORRENT


def test_torrent_candidates_unrelated_files_ignored(tmp_path):
    resolver = PreviewResolver(_Ctx())
    (tmp_path / "movie.mkv").write_bytes(b"x")
    (tmp_path / ".torrent_abc.tmp").write_bytes(b"not-torrent")
    download = _FakeDownload(str(tmp_path / "missing.torrent"))
    assert str(tmp_path / "movie.mkv") not in resolver.filenames(download)


def test_torrent_candidates_from_source_url(tmp_path):
    resolver = PreviewResolver(_Ctx())
    src = tmp_path / "the-movie-source.torrent"
    src.write_bytes(b"d8:announce0e")
    download = _FakeDownload(
        str(tmp_path / "The Movie"), detected_type="torrent", url=str(src)
    )
    assert resolver.type_of_download(download) == PreviewType.TORRENT