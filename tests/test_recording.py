from datetime import datetime
from pathlib import Path

import pytest

from livedigest import recording


def test_parse_name_with_underscore_anchor_and_segment():
    info = recording.parse_name(
        Path("/x/小红书直播/a_b/a_b_2026-10-17_20-01-02_003.ts")
    )
    assert info.anchor == "a_b"
    assert info.started_at == datetime(2026, 10, 17, 20, 1, 2)
    assert info.segment == 3


def test_parse_name_falls_back_to_folder():
    info = recording.parse_name(Path("/x/主播甲/random.mp4"))
    assert (info.anchor, info.started_at) == ("主播甲", None)


def test_fingerprint_changes_with_content(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"1" * 100)
    first = recording.fingerprint(a)
    a.write_bytes(b"2" * 100)
    assert recording.fingerprint(a) != first


@pytest.mark.parametrize(
    ("kind", "platform", "phrase"),
    [
        ("live", "douyin", "一场抖音直播"),
        ("live", "bilibili", "一场B站直播"),
        ("live", "xiaohongshu", "一场小红书直播"),
        ("live", "youtube", "一场YouTube直播"),
        ("live", "小红书直播", "一场小红书直播"),
        ("live", "抖音直播", "一场抖音直播"),
        ("live", "B站直播", "一场B站直播"),
        ("live", "recordings", "一场直播"),
        ("live", None, "一场直播"),
        ("video", "douyin-video", "一条抖音短视频"),
    ],
)
def test_source_phrase_names_the_platform(kind, platform, phrase):
    assert recording.source_phrase(kind, platform) == phrase
