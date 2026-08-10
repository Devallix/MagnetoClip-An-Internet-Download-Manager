import json

from magnetoclip.engine.resume.mclip import (
    MClipState,
    SegmentState,
    reconcile_part_sizes,
)


def _state():
    return MClipState(
        url="https://example.com/a.bin",
        file_path=r"C:\Downloads\a.bin",
        total_size=100,
        etag='"x"',
        headers={"User-Agent": "MagnetoClip"},
        hash_algo="sha256",
        hash_expected="abc",
        state="downloading",
        segments=[
            SegmentState(index=0, start=0, end=49, written=50, status="completed"),
            SegmentState(index=1, start=50, end=99, written=20),
        ],
    )


def test_roundtrip(tmp_path):
    state = _state()
    sidecar = tmp_path / "a.bin.mclip"
    sidecar.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    loaded = MClipState.load(sidecar)
    assert loaded.url == state.url
    assert loaded.total_size == 100
    assert loaded.bytes_downloaded == 70
    assert loaded.segments[1].written == 20
    assert loaded.hash_algo == "sha256"


def test_part_paths():
    state = _state()
    assert str(state.part_path(0)).endswith("a.bin.part0")
    assert state.part_paths[0].name == "a.bin.part0"


def test_reconcile_part_sizes(tmp_path):
    state = _state()
    state.file_path = str(tmp_path / "a.bin")
    state.part_path(1).write_bytes(b"x" * 30)
    reconcile_part_sizes(state)
    assert state.segments[1].written == 30
    assert state.segments[1].status == "pending"


def test_reconcile_completes_segment(tmp_path):
    state = _state()
    state.file_path = str(tmp_path / "a.bin")
    state.part_path(1).write_bytes(b"x" * 50)
    reconcile_part_sizes(state)
    assert state.segments[1].written == 50
    assert state.segments[1].complete
    assert state.segments[1].status == "completed"


def test_save_and_load(tmp_path):
    state = _state()
    state.file_path = str(tmp_path / "a.bin")
    state.save()
    loaded = MClipState.load(MClipState.sidecar_for(state.file_path))
    assert loaded.file_path == state.file_path
    assert len(loaded.segments) == 2
