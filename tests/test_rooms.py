from pathlib import Path

import pytest

from livedigest import rooms
from livedigest.rooms import Room, add_room, load_rooms, normalize, set_enabled


def no_network(url):
    raise AssertionError(f"unexpected lookup of {url}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "【来看直播】 https://live.bilibili.com/1000002?broadcast_type=0&x=1 快来",
            ("bilibili", "https://live.bilibili.com/1000002"),
        ),
        (
            "https://live.bilibili.com/h5/123",
            ("bilibili", "https://live.bilibili.com/123"),
        ),
        (
            "7- 打开抖音搜索 https://v.douyin.com/A_bCdEfGhIj/ 2@5.com",
            ("douyin", "https://v.douyin.com/A_bCdEfGhIj/"),
        ),
        (
            "https://live.douyin.com/100000000001?enter_from=x",
            ("douyin", "https://live.douyin.com/100000000001"),
        ),
        (
            "主播正在直播，https://xhslink.com/m/AbCdEf，复制后打开小红书",
            ("xiaohongshu", "https://xhslink.com/m/AbCdEf"),
        ),
        (
            "https://www.xiaohongshu.com/user/profile/abc?xsec=1&host_id=h9&t=2",
            ("xiaohongshu", "https://www.xiaohongshu.com/user/profile/abc?host_id=h9"),
        ),
        (
            "https://youtu.be/abcDEF12345?si=share",
            ("youtube", "https://www.youtube.com/watch?v=abcDEF12345"),
        ),
        (
            "https://www.youtube.com/live/abcDEF12345?feature=share",
            ("youtube", "https://www.youtube.com/watch?v=abcDEF12345"),
        ),
        (
            "https://m.youtube.com/watch?v=abcDEF12345&t=10",
            ("youtube", "https://www.youtube.com/watch?v=abcDEF12345"),
        ),
        (
            "https://www.youtube.com/@ExampleChannel/live/",
            ("youtube", "https://www.youtube.com/@ExampleChannel/live"),
        ),
    ],
)
def test_normalize(text, expected):
    assert normalize(text, resolve=no_network) == expected


def test_normalize_resolves_bilibili_short_links():
    def resolve(url):
        assert url == "https://b23.tv/AbC123"
        return "https://live.bilibili.com/42?share_source=copy"

    assert normalize("看 https://b23.tv/AbC123", resolve) == (
        "bilibili",
        "https://live.bilibili.com/42",
    )


def test_normalize_rejects_short_link_to_a_video():
    with pytest.raises(ValueError, match="not a Bilibili live room"):
        normalize("https://b23.tv/x", lambda url: "https://www.bilibili.com/video/BV1")


@pytest.mark.parametrize(
    "text", ["no link here", "https://example.com/live/1", "https://youtu.be/bad"]
)
def test_normalize_rejects(text):
    with pytest.raises(ValueError):
        normalize(text, resolve=no_network)


def test_add_load_and_disable_round_trip(tmp_path):
    path = tmp_path / "rooms.toml"
    room, added = add_room(
        path, "https://live.bilibili.com/1", keep="video", name='讲座 "A"\\b'
    )
    assert added and room.platform == "bilibili"
    _, added = add_room(path, "https://xhslink.com/m/Z", resolve=no_network)
    assert added
    _, added = add_room(path, "again https://live.bilibili.com/1?x=2")
    assert not added

    loaded = load_rooms(path)
    assert loaded == [
        Room("https://live.bilibili.com/1", "bilibili", '讲座 "A"\\b', "video"),
        Room("https://xhslink.com/m/Z", "xiaohongshu"),
    ]
    assert path.read_text(encoding="utf-8").startswith(rooms.HEADER)

    assert set_enabled(path, "https://xhslink.com/m/Z", False)
    assert not load_rooms(path)[1].enabled
    assert not set_enabled(path, "https://nowhere", False)

    _, added = add_room(path, "https://xhslink.com/m/Z")
    assert added and load_rooms(path)[1].enabled


def test_load_rejects_bad_values(tmp_path):
    path = tmp_path / "rooms.toml"
    path.write_text('[[room]]\nurl = "u"\nplatform = "twitch"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="platform"):
        load_rooms(path)
    path.write_text(
        '[[room]]\nurl = "u"\nplatform = "douyin"\nkeep = "all"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="keep"):
        load_rooms(path)


def test_missing_file_is_empty(tmp_path):
    assert load_rooms(tmp_path / "none.toml") == []


def test_example_file_parses():
    example = Path(__file__).parents[1] / "rooms.example.toml"
    assert len(load_rooms(example)) == 3


def test_single_use_only_for_xhs_share_links():
    assert Room("https://xhslink.com/m/a", "xiaohongshu").single_use
    assert not Room(
        "https://www.xiaohongshu.com/user/profile/a", "xiaohongshu"
    ).single_use
    assert not Room("https://live.bilibili.com/1", "bilibili").single_use
