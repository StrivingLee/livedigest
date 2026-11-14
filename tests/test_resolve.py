import json
import subprocess
from types import SimpleNamespace

import pytest

from livedigest import resolve as resolve_module
from livedigest.capture import CaptureSettings
from livedigest.resolve import (
    ResolveError,
    StreamInfo,
    candidate_urls,
    pick_url,
    resolve,
)
from livedigest.rooms import Room


def stream(**fields):
    defaults = dict(
        platform="x",
        anchor_name="主播",
        is_live=True,
        title="标题",
        flv_url=None,
        m3u8_url=None,
        record_url=None,
    )
    return SimpleNamespace(**(defaults | fields))


class FakeLive:
    calls = []
    data = {"anchor_name": "主播"}
    result = stream(flv_url="https://cdn/x.flv")
    error = None

    def __init__(self, proxy_addr=None, cookies=None):
        FakeLive.calls.append(("init", proxy_addr, cookies))

    async def fetch_web_stream_data(self, url):
        FakeLive.calls.append(("web", url))
        if FakeLive.error:
            raise FakeLive.error
        return FakeLive.data

    async def fetch_app_stream_data(self, url):
        FakeLive.calls.append(("app", url))
        return FakeLive.data

    async def fetch_stream_url(self, data, quality):
        FakeLive.calls.append(("stream", quality))
        return FakeLive.result


@pytest.fixture
def fake(monkeypatch):
    FakeLive.calls = []
    FakeLive.data = {"anchor_name": "主播"}
    FakeLive.result = stream(flv_url="https://cdn/x.flv")
    FakeLive.error = None
    monkeypatch.setattr(resolve_module, "_stream_class", lambda platform: FakeLive)
    monkeypatch.setattr(
        resolve_module,
        "bilibili_urls",
        lambda url, settings: ["https://a.cdn/live.flv", "https://b.cdn/live.m3u8"],
    )
    return FakeLive


def test_live_room_prefers_flv(fake):
    fake.result = stream(flv_url="https://cdn/x.flv", m3u8_url="https://cdn/x.m3u8")
    settings = CaptureSettings(cookies={"douyin": "c=1"}, proxy="http://p")
    info = resolve(Room("https://live.douyin.com/1", "douyin"), settings)
    assert info.is_live and info.url == "https://cdn/x.flv"
    assert info.urls == ("https://cdn/x.flv", "https://cdn/x.m3u8")
    assert (info.anchor, info.title, info.platform) == ("主播", "标题", "douyin")
    assert fake.calls[0] == ("init", "http://p", "c=1")
    assert ("stream", "LD") in fake.calls


def test_quality_is_best_for_video_rooms_or_when_asked(fake):
    resolve(
        Room("https://live.douyin.com/1", "douyin", keep="video"), CaptureSettings()
    )
    resolve(
        Room("https://live.douyin.com/1", "douyin"), CaptureSettings(quality="best")
    )
    assert [c[1] for c in fake.calls if c[0] == "stream"] == ["OD", "OD"]
    with pytest.raises(ValueError, match="quality"):
        CaptureSettings(quality="high")


def test_bilibili_lists_every_candidate(fake, monkeypatch):
    fake.result = stream(record_url="https://a.cdn/other-token.flv")
    room = Room("https://live.bilibili.com/1", "bilibili")
    info = resolve(room, CaptureSettings())
    # streamget's own pick shares a host with a listed candidate: dropped.
    assert info.urls == ("https://a.cdn/live.flv", "https://b.cdn/live.m3u8")
    assert info.headers["User-Agent"] == resolve_module.DESKTOP_UA

    def broken(url, settings):
        raise OSError("timed out")

    monkeypatch.setattr(resolve_module, "bilibili_urls", broken)
    assert resolve(room, CaptureSettings()).urls == ("https://a.cdn/other-token.flv",)


def test_bilibili_urls_parse_both_apis(monkeypatch):
    def flv(host, fmt):
        return {
            "format_name": fmt,
            "codec": [
                {
                    "base_url": "/live/x.flv?",
                    "url_info": [{"host": f"https://{host}", "extra": "k=1"}],
                }
            ],
        }

    replies = {
        "/room/v1/Room/room_init": {"room_id": 1000001},
        "/xlive/web-room/v2/index/getRoomPlayInfo": {
            "playurl_info": {
                "playurl": {
                    "stream": [
                        {"format": [flv("h1", "ts")]},
                        {"format": [flv("f1", "flv"), flv("f1", "flv")]},
                    ]
                }
            }
        },
        "/room/v1/Room/playUrl": {
            "durl": [{"url": "https://f1/other"}, {"url": "https://f2/x.flv"}]
        },
    }
    seen = []

    def get(path, params, settings):
        seen.append((path, params))
        return replies[path]

    monkeypatch.setattr(resolve_module, "_bilibili_get", get)
    urls = resolve_module.bilibili_urls(
        "https://live.bilibili.com/6?x=1", CaptureSettings()
    )
    assert urls == [
        "https://f1/live/x.flv?k=1",
        "https://f2/x.flv",
        "https://h1/live/x.flv?k=1",
    ]
    assert seen[0][1] == {"id": "6"}
    assert seen[1][1]["room_id"] == 1000001 and seen[2][1]["cid"] == 1000001


def test_stream_info_url_and_candidates():
    assert StreamInfo(True, "x", "", "", "u").urls == ("u",)
    assert StreamInfo(True, "x", "", "", urls=["a", "b"]).url == "a"
    assert StreamInfo(False, "x", "", "").urls == ()


def test_douyin_share_links_use_app_api(fake):
    resolve(Room("https://v.douyin.com/abc/", "douyin"), CaptureSettings())
    resolve(Room("https://live.douyin.com/123", "douyin"), CaptureSettings())
    assert [c[0] for c in fake.calls if c[0] in ("app", "web")] == ["app", "web"]


def test_offline_room(fake):
    fake.result = stream(is_live=False, anchor_name="", title=None)
    info = resolve(
        Room("https://xhslink.com/m/a", "xiaohongshu", "名字"), CaptureSettings()
    )
    assert not info.is_live and info.url is None and info.anchor == "名字"


def test_bilibili_swallowed_error_is_reported(fake):
    fake.data = {"anchor_name": "", "live_status": False}
    with pytest.raises(ResolveError):
        resolve(Room("https://live.bilibili.com/1", "bilibili"), CaptureSettings())


def test_streamget_failure_becomes_resolve_error(fake):
    fake.error = KeyError("data")
    with pytest.raises(ResolveError, match="lookup failed"):
        resolve(Room("https://live.douyin.com/1", "douyin"), CaptureSettings())


def test_live_without_url_is_an_error(fake):
    fake.result = stream()
    with pytest.raises(ResolveError, match="no stream URL"):
        resolve(Room("https://live.douyin.com/1", "douyin"), CaptureSettings())


def test_pick_url():
    assert pick_url("douyin", stream(flv_url="f", m3u8_url="h")) == "f"
    h265 = stream(flv_url="https://x/a.flv?codec=h265", m3u8_url="h")
    assert pick_url("douyin", h265) == "h"
    assert pick_url("bilibili", stream(record_url="r")) == "r"
    assert pick_url("youtube", stream(m3u8_url="master", record_url="variant")) == (
        "variant"
    )


def test_candidate_urls():
    both = stream(flv_url="f", m3u8_url="h", record_url="h")
    assert candidate_urls("douyin", both) == ["f", "h"]
    h265 = stream(flv_url="https://x/a.flv?codec=h265", m3u8_url="h")
    assert candidate_urls("douyin", h265) == ["h"]
    yt = stream(m3u8_url="master", record_url="variant")
    assert candidate_urls("youtube", yt) == ["variant", "master"]


def test_youtube_falls_back_to_ytdlp(fake, monkeypatch):
    fake.error = Exception("Please log in")
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        out = {
            "live_status": "is_live",
            "channel": "Chan",
            "title": "24/7",
            "url": "https://manifest/x.m3u8",
            "http_headers": {"User-Agent": "UA"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(out), "")

    monkeypatch.setattr(resolve_module.subprocess, "run", run)
    info = resolve(
        Room("https://www.youtube.com/@c/live", "youtube"), CaptureSettings()
    )
    assert info.is_live and info.url == "https://manifest/x.m3u8"
    assert info.headers == {"User-Agent": "UA"} and info.anchor == "Chan"
    assert "--ignore-config" in seen[0]


def test_ytdlp_not_live_and_failure(fake, monkeypatch):
    fake.error = Exception("x")
    stderr = {"value": "ERROR: [youtube] x: This live event will begin in 3 hours."}
    monkeypatch.setattr(
        resolve_module.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(
            command, 1, "", stderr["value"]
        ),
    )
    room = Room("https://www.youtube.com/watch?v=abcDEF12345", "youtube")
    assert not resolve(room, CaptureSettings()).is_live
    stderr["value"] = "ERROR: Sign in to confirm you're not a bot"
    with pytest.raises(ResolveError, match="bot"):
        resolve(room, CaptureSettings())
