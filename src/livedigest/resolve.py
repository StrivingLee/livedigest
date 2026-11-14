"""Turn a room link into a playable stream URL using streamget."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from livedigest.rooms import Room

if TYPE_CHECKING:
    from livedigest.capture import CaptureSettings

log = logging.getLogger(__name__)

RESOLVE_TIMEOUT = 60
YTDLP_FORMAT = "b[acodec!=none][vcodec!=none]/b[acodec!=none]/ba"
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BILIBILI_REFERER = "https://live.bilibili.com/"
BILIBILI_API = "https://api.live.bilibili.com"


class ResolveError(Exception):
    """The room's state could not be determined (network, parse, blocked)."""


@dataclass
class StreamInfo:
    """``urls`` holds every candidate, best first; ``url`` is the one to use.

    Both are filled from either: given only ``url`` it is the sole candidate,
    given only ``urls`` the first is used.
    """

    is_live: bool
    platform: str
    anchor: str
    title: str
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.urls = tuple(self.urls)
        if self.url and not self.urls:
            self.urls = (self.url,)
        elif self.urls and not self.url:
            self.url = self.urls[0]


def host_of(url: str) -> str:
    """The server a candidate URL points at, used to remember failures."""
    return urllib.parse.urlsplit(url).netloc or url


def _stream_class(platform: str) -> Any:
    from streamget import (
        BilibiliLiveStream,
        DouyinLiveStream,
        RedNoteLiveStream,
        YoutubeLiveStream,
    )

    return {
        "douyin": DouyinLiveStream,
        "bilibili": BilibiliLiveStream,
        "xiaohongshu": RedNoteLiveStream,
        "youtube": YoutubeLiveStream,
    }[platform]


async def _fetch(room: Room, settings: CaptureSettings) -> Any:
    live = _stream_class(room.platform)(
        proxy_addr=settings.proxy, cookies=settings.cookies.get(room.platform)
    )
    url = room.url
    if room.platform == "douyin" and "live.douyin.com" not in url:
        # Share links and profile pages only work through the app API.
        data = await live.fetch_app_stream_data(url)
    elif room.platform == "xiaohongshu":
        data = await live.fetch_app_stream_data(url)
    else:
        data = await live.fetch_web_stream_data(url)
    if room.platform == "bilibili" and not data.get("anchor_name"):
        # streamget prints and swallows Bilibili errors, reporting "offline".
        raise ResolveError(f"Bilibili room lookup failed for {url}")
    return await live.fetch_stream_url(data, streamget_quality(room, settings))


def streamget_quality(room: Room, settings: CaptureSettings) -> str:
    """streamget's "LD" is its smallest stream on Douyin (720p there; its "SD"
    is 540p). Bilibili serves guests one quality whatever is asked."""
    if settings.quality == "best" or room.keep == "video":
        return "OD"
    return "LD"


def pick_url(platform: str, stream: Any) -> str | None:
    """FLV is one long HTTP response, which ffmpeg reads with the least overhead;
    YouTube serves HLS only; its record_url is the chosen variant playlist,
    which avoids ffmpeg probing every variant of the master playlist. Douyin's
    H.265 FLV is not standard FLV, so H.265 rooms fall back to HLS as the old
    recorder did.
    """
    flv, hls = stream.flv_url, stream.m3u8_url
    if platform == "youtube":
        return stream.record_url or hls
    if platform == "douyin" and flv:
        codec = urllib.parse.parse_qs(urllib.parse.urlsplit(flv).query).get("codec")
        if codec and codec[0] == "h265" and hls:
            return hls
    return flv or stream.record_url or hls


def candidate_urls(platform: str, stream: Any) -> list[str]:
    """``pick_url``'s choice first, then the distinct alternates streamget gave.

    Douyin's H.265 FLV is left out entirely: ffmpeg cannot read it.
    """
    first = pick_url(platform, stream)
    flv = stream.flv_url
    if platform == "douyin" and flv and first != flv:
        codec = urllib.parse.parse_qs(urllib.parse.urlsplit(flv).query).get("codec")
        if codec and codec[0] == "h265":
            flv = None
    ordered = [first, flv, stream.record_url, stream.m3u8_url]
    return list(dict.fromkeys(url for url in ordered if url))


def _bilibili_get(path: str, params: dict, settings: CaptureSettings) -> dict:
    headers = {"User-Agent": DESKTOP_UA, "Referer": BILIBILI_REFERER}
    if cookie := settings.cookies.get("bilibili"):
        headers["Cookie"] = cookie
    url = f"{BILIBILI_API}{path}?{urllib.parse.urlencode(params)}"
    handlers = []
    if settings.proxy:
        handlers.append(
            urllib.request.ProxyHandler(
                {"http": settings.proxy, "https": settings.proxy}
            )
        )
    opener = urllib.request.build_opener(*handlers)
    with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as r:
        data = json.load(r)
    if data.get("code") != 0:
        raise ValueError(f"{path}: {data.get('code')} {data.get('message')}")
    return data.get("data") or {}


def bilibili_urls(room_url: str, settings: CaptureSettings) -> list[str]:
    """Every stream URL Bilibili offers for a room: FLV first, then HLS.

    The APIs return several CDN hosts per room and some of them refuse or cut
    off guests, so all are kept for the watcher to choose from. Duplicates
    (same host and format) are dropped. Guests are served one quality (超清)
    whatever ``qn`` asks for, so the highest is requested.
    """
    room_id = urllib.parse.urlsplit(room_url).path.rstrip("/").rsplit("/", 1)[-1]
    # Short ids (e.g. /6) only work with room_init; the play APIs need the real one.
    real_id = _bilibili_get("/room/v1/Room/room_init", {"id": room_id}, settings)[
        "room_id"
    ]
    flv: list[str] = []
    hls: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add(url: str, fmt: str) -> None:
        key = (host_of(url), fmt)
        if key not in seen:
            seen.add(key)
            (flv if fmt == "flv" else hls).append(url)

    params = {
        "room_id": real_id,
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0",
        "qn": 10000,
        "platform": "web",
        "ptype": 8,
    }
    info = _bilibili_get("/xlive/web-room/v2/index/getRoomPlayInfo", params, settings)
    playurl = (info.get("playurl_info") or {}).get("playurl") or {}
    for stream in playurl.get("stream") or []:
        for fmt in stream.get("format") or []:
            for codec in fmt.get("codec") or []:
                for entry in codec.get("url_info") or []:
                    url = entry["host"] + codec["base_url"] + entry["extra"]
                    add(url, fmt.get("format_name", ""))
    params = {"cid": real_id, "qn": 10000, "platform": "web"}
    for entry in (
        _bilibili_get("/room/v1/Room/playUrl", params, settings).get("durl") or []
    ):
        add(entry["url"], "flv")
    return flv + hls


def _ytdlp(room: Room, settings: CaptureSettings) -> StreamInfo:
    """YouTube pages change often; yt-dlp is updated faster than streamget."""
    binary = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
    # --ignore-config: a user config may add browser cookies or a download
    # format; this lookup must behave the same on every machine.
    command = [binary, "--ignore-config", "--no-playlist", "--no-warnings"]
    command += ["-f", YTDLP_FORMAT, "-J"]
    if settings.proxy:
        command += ["--proxy", settings.proxy]
    try:
        result = subprocess.run(
            [*command, room.url],
            capture_output=True,
            text=True,
            timeout=RESOLVE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolveError(f"yt-dlp failed: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()[-1:] or ["no output"]
        if "will begin" in message[0] or "not currently live" in message[0]:
            return StreamInfo(False, "youtube", room.name, "")
        raise ResolveError(f"yt-dlp: {message[0]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ResolveError(f"yt-dlp printed invalid JSON: {exc}") from exc
    is_live = data.get("live_status") == "is_live" or data.get("is_live") is True
    return StreamInfo(
        is_live=is_live,
        platform="youtube",
        anchor=data.get("channel") or data.get("uploader") or room.name,
        title=data.get("title") or "",
        url=data.get("url") if is_live else None,
        headers=data.get("http_headers") or {},
    )


def resolve(room: Room, settings: CaptureSettings) -> StreamInfo:
    try:
        stream = asyncio.run(asyncio.wait_for(_fetch(room, settings), RESOLVE_TIMEOUT))
    except ResolveError:
        raise
    # streamget raises bare Exception (and KeyError/JSON errors from pages it
    # failed to parse), so nothing narrower identifies a failed lookup.
    except Exception as exc:  # noqa: BLE001
        if room.platform == "youtube":
            log.info("streamget failed for %s (%s); trying yt-dlp", room.url, exc)
            return _ytdlp(room, settings)
        raise ResolveError(
            f"{room.platform} lookup failed for {room.url}: {exc}"
        ) from exc
    anchor = stream.anchor_name or room.name or ""
    if not stream.is_live:
        return StreamInfo(False, room.platform, anchor, stream.title or "")
    urls = candidate_urls(room.platform, stream)
    headers = {}
    if room.platform == "bilibili":
        headers = {"User-Agent": DESKTOP_UA}
        try:
            listed = bilibili_urls(room.url, settings)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Bilibili candidate lookup failed (%s); using streamget's", exc)
        else:
            hosts = {host_of(url) for url in listed}
            urls = listed + [url for url in urls if host_of(url) not in hosts]
    if not urls:
        raise ResolveError(f"{room.url} is live but no stream URL was returned")
    return StreamInfo(
        True, room.platform, anchor, stream.title or "", headers=headers, urls=urls
    )
