"""Facts about a media file: naming, duration, identity, saved metadata."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PLATFORM_NAMES = {
    "douyin": "抖音",
    "bilibili": "B站",
    "xiaohongshu": "小红书",
    "youtube": "YouTube",
}
# Files under <recordings>/douyin-video/ are short videos, not broadcasts.
VIDEO_FOLDER = "douyin-video"
# Older recordings (and other recorders) are named
# <anchor>_[<title>_]<YYYY-MM-DD_HH-MM-SS>[_NNN].<ext>
_NAME_RE = re.compile(
    r"^(?P<prefix>.+)_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})"
    r"(?:_(?P<segment>\d{3}))?$"
)


def source_phrase(kind: str | None, platform: str | None) -> str:
    """How the prompts name what was recorded, e.g. ``一场抖音直播``.

    ``platform`` is a platform id (``douyin`` ...) for captured broadcasts, or
    the ``<平台>直播`` folder that older recordings were saved under. Anything
    else falls back to a platform-neutral phrase.
    """
    if kind == "video":
        return "一条抖音短视频"
    if platform in PLATFORM_NAMES:
        return f"一场{PLATFORM_NAMES[platform]}直播"
    if platform and platform.endswith("直播") and len(platform) > 2:
        return f"一场{platform}"
    return "一场直播"


@dataclass(frozen=True)
class NameInfo:
    anchor: str
    started_at: datetime | None
    segment: int | None


def parse_name(path: Path) -> NameInfo:
    match = _NAME_RE.match(path.stem)
    if not match:
        return NameInfo(anchor=path.parent.name, started_at=None, segment=None)
    started = datetime.strptime(match["stamp"], "%Y-%m-%d_%H-%M-%S")
    segment = int(match["segment"]) if match["segment"] else None
    return NameInfo(anchor=match["prefix"], started_at=started, segment=segment)


def fingerprint(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    """Cheap content identity: size plus hashes of the head and tail."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as fh:
        digest.update(fh.read(chunk))
        if size > chunk:
            fh.seek(max(size - chunk, chunk))
            digest.update(fh.read(chunk))
    return digest.hexdigest()[:16]


def read_info(path: Path) -> dict:
    """Metadata saved beside a downloaded video as ``<stem>.info.json``.

    It is optional: a missing or unreadable file gives ``{}``.
    """
    try:
        info = json.loads(path.with_suffix(".info.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}
