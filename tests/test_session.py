import json

from livedigest.session import (
    STATE_ENDED,
    Session,
    epoch,
    find_sessions,
    iso,
    safe_name,
)

T0 = 1_800_000_000.0


def make(tmp_path, keep="auto"):
    return Session.create(
        tmp_path,
        "bilibili",
        "主播/a:b",
        "标题",
        "https://live.bilibili.com/1",
        keep,
        T0,
    )


def test_create_lays_out_directories(tmp_path):
    session = make(tmp_path)
    assert session.dir.parent == tmp_path / "bilibili" / "主播_a_b"
    assert session.dir.name.count("_") == 1 and len(session.dir.name) == 19
    assert session.audio_dir.is_dir() and session.frames_dir.is_dir()
    assert not session.video_dir.exists()
    assert make(tmp_path / "v", keep="video").video_dir.is_dir()
    data = json.loads((session.dir / "session.json").read_text(encoding="utf-8"))
    assert data["state"] == "live" and data["parts"] == []
    assert data["id"] == session.dir.relative_to(tmp_path).as_posix()
    assert not (session.dir / "session.json.tmp").exists()


def test_parts_offsets_and_round_trip(tmp_path):
    session = make(tmp_path)
    first = session.start_part(T0 + 2)
    session.end_part(T0 + 100)
    second = session.start_part(T0 + 160.5)
    session.save()
    assert (first.index, first.offset_seconds) == (1, 2.0)
    assert (second.index, second.offset_seconds) == (2, 160.5)

    loaded = Session.load(session.dir)
    assert loaded.parts == session.parts
    assert loaded.part(2).ended_at is None
    assert loaded.current_part.index == 2


def test_end_uses_last_activity(tmp_path):
    session = make(tmp_path)
    session.start_part(T0)
    session.touch(T0 + 300)
    session.end()
    assert session.state == STATE_ENDED
    assert epoch(session.ended_at) == T0 + 300
    assert epoch(session.parts[0].ended_at) == T0 + 300


def test_find_sessions_filters_by_state(tmp_path):
    live = make(tmp_path)
    ended = Session.create(tmp_path, "douyin", "b", "", "u", "audio", T0 + 5)
    ended.end()
    ended.save()
    assert [s.id for s in find_sessions(tmp_path, "live")] == [live.id]
    assert len(find_sessions(tmp_path)) == 2


def test_helpers():
    assert safe_name("  ..  ") == "unknown"
    assert safe_name("a\x00b") == "a_b"
    assert epoch(iso(T0)) == T0
