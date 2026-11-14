"""The user's list of live rooms to watch, kept in ``rooms.toml``."""

from __future__ import annotations

import json
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PLATFORMS = ("douyin", "bilibili", "xiaohongshu", "youtube")
KEEP_MODES = ("auto", "audio", "video")

# Share messages wrap the link in CJK text and punctuation.
_URL_RE = re.compile(r"https?://[^\s，,。！!】）)\"'“”]+")
_BILI_ROOM_RE = re.compile(r"^https?://live\.bilibili\.com/(?:h5/)?(\d+)")
_YT_ID_RE = re.compile(r"^[\w-]{11}$")

HEADER = "# Live rooms for `livedigest watch`; keep = auto | audio | video.\n"


@dataclass
class Room:
    url: str
    platform: str
    name: str = ""
    keep: str = "auto"
    enabled: bool = True

    @property
    def single_use(self) -> bool:
        """Xiaohongshu share links work once, so a finished room is retired."""
        return self.platform == "xiaohongshu" and "xhslink.com" in self.url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_redirect(url: str) -> str:
    """Return where a short link points without following it further."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="HEAD")
    try:
        with opener.open(request, timeout=10) as response:
            location = response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"could not open {url}: {exc}") from exc
    if not location:
        raise ValueError(f"{url} did not redirect anywhere")
    return location


def normalize(
    text: str, resolve: Callable[[str], str] = resolve_redirect
) -> tuple[str, str]:
    """Find the live-room link in a pasted share message.

    Links are reduced to one canonical spelling so pasting the same room twice
    does not add it twice; tracking query strings are dropped.
    """
    match = _URL_RE.search(text)
    if not match:
        raise ValueError(f"no URL found in: {text!r}")
    url = match.group(0)
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "b23.tv":
        url = resolve(url)
        if not _BILI_ROOM_RE.match(url):
            raise ValueError(f"not a Bilibili live room: {url}")
    if room := _BILI_ROOM_RE.match(url):
        return "bilibili", f"https://live.bilibili.com/{room.group(1)}"
    if host == "v.douyin.com":
        return "douyin", f"https://v.douyin.com{parts.path.rstrip('/')}/"
    if host in ("live.douyin.com", "douyin.com"):
        return "douyin", f"https://{parts.netloc}{parts.path}"
    if host == "xhslink.com":
        return "xiaohongshu", url
    if host == "xiaohongshu.com":
        # streamget reads host_id from the query; everything else is tracking.
        query = urllib.parse.parse_qs(parts.query)
        keep = f"?host_id={query['host_id'][0]}" if "host_id" in query else ""
        return "xiaohongshu", f"https://www.xiaohongshu.com{parts.path}{keep}"
    if host in ("youtube.com", "youtu.be"):
        return "youtube", _youtube_url(host, parts)
    raise ValueError(f"unsupported live platform: {url}")


def _youtube_url(host: str, parts: urllib.parse.SplitResult) -> str:
    path = parts.path.rstrip("/")
    video_id = None
    if host == "youtu.be":
        video_id = path.lstrip("/")
    elif path.startswith("/live/"):
        video_id = path.removeprefix("/live/")
    elif path == "/watch":
        video_id = urllib.parse.parse_qs(parts.query).get("v", [""])[0]
    if video_id is not None:
        if not _YT_ID_RE.match(video_id):
            raise ValueError(f"not a YouTube video id: {video_id!r}")
        return f"https://www.youtube.com/watch?v={video_id}"
    # Channel pages such as /@name/live follow whatever the channel airs next.
    return f"https://www.youtube.com{path}"


def load_rooms(path: Path) -> list[Room]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    rooms = []
    for entry in data.get("room", []):
        room = Room(
            url=entry["url"],
            platform=entry["platform"],
            name=entry.get("name", ""),
            keep=entry.get("keep", "auto"),
            enabled=entry.get("enabled", True),
        )
        if room.platform not in PLATFORMS:
            raise ValueError(f"{path}: unknown platform {room.platform!r}")
        if room.keep not in KEEP_MODES:
            raise ValueError(f"{path}: keep must be one of {KEEP_MODES}")
        rooms.append(room)
    return rooms


def _toml_string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def save_rooms(path: Path, rooms: list[Room]) -> None:
    """Rewrite the file; comments other than the header are not preserved."""
    blocks = []
    for room in rooms:
        lines = [
            "[[room]]",
            f"url = {_toml_string(room.url)}",
            f"platform = {_toml_string(room.platform)}",
        ]
        if room.name:
            lines.append(f"name = {_toml_string(room.name)}")
        lines.append(f"keep = {_toml_string(room.keep)}")
        lines.append(f"enabled = {'true' if room.enabled else 'false'}")
        blocks.append("\n".join(lines) + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(HEADER + "\n" + "\n".join(blocks), encoding="utf-8")
    tmp.replace(path)


def add_room(
    path: Path,
    text: str,
    keep: str = "auto",
    name: str | None = None,
    resolve: Callable[[str], str] = resolve_redirect,
) -> tuple[Room, bool]:
    """Add a room from a share message, or re-enable it if already listed.

    Returns the room and whether anything changed.
    """
    if keep not in KEEP_MODES:
        raise ValueError(f"keep must be one of {KEEP_MODES}")
    platform, url = normalize(text, resolve)
    rooms = load_rooms(path)
    for room in rooms:
        if room.url == url:
            if room.enabled:
                return room, False
            room.enabled = True
            save_rooms(path, rooms)
            return room, True
    room = Room(url=url, platform=platform, name=name or "", keep=keep)
    rooms.append(room)
    save_rooms(path, rooms)
    return room, True


def set_enabled(path: Path, url: str, enabled: bool) -> bool:
    """Returns False when no room has this URL."""
    rooms = load_rooms(path)
    for room in rooms:
        if room.url == url:
            room.enabled = enabled
            save_rooms(path, rooms)
            return True
    return False
