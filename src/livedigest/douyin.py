"""Download a Douyin video and its metadata through OpenCLI.

Douyin signs its video-detail API inside the page, so plain HTTP clients
(yt-dlp included) get 403. ``opencli douyin detail`` runs in the user's
logged-in Chrome through the Browser Bridge extension and returns the
metadata with a short-lived ``play_url``. The CDN behind that URL answers 403
(``Forbid_code 020200``) without Douyin's Referer, and from an IP outside
mainland China, so ``douyinvod.com`` must not go through an overseas proxy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from livedigest import recording
from livedigest.config import Settings

log = logging.getLogger(__name__)

FOLDER = "douyin-video"
VIDEO_URL = "https://www.douyin.com/video/{id}"
REFERER = "https://www.douyin.com/"

_URL_RE = re.compile(r"https?://[^\s，,。】]+")
_ID_RE = re.compile(r"^\d{15,20}$")
_UNSAFE = re.compile(r'[/\\:*?"<>|\s]+')
_CDN_FIELDS = re.compile(r"(Forbid_code|Client_ip): ([\w.]+)")


class DouyinError(Exception):
    """The video could not be resolved or downloaded."""


def extract_target(text: str) -> str:
    """The link or numeric video ID in a pasted share message."""
    if match := _URL_RE.search(text):
        return match.group(0)
    if _ID_RE.match(text.strip()):
        return text.strip()
    raise DouyinError(f"no Douyin link or video ID in: {text!r}")


def _node_version(path: Path) -> tuple[int, ...]:
    # ~/.nvm/versions/node/v26.12.1/bin/opencli
    return tuple(int(n) for n in re.findall(r"\d+", path.parents[1].name))


def find_opencli(configured: str = "") -> Path:
    """``[douyin] opencli``, ``$OPENCLI``, PATH, then the newest nvm Node."""
    candidates = [configured, os.environ.get("OPENCLI", ""), shutil.which("opencli")]
    candidates += sorted(
        Path.home().glob(".nvm/versions/node/*/bin/opencli"),
        key=_node_version,
        reverse=True,
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    raise DouyinError("opencli not found; set [douyin] opencli in livedigest.toml")


def fetch_detail(
    target: str,
    opencli: Path,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    attempts: int = 2,
) -> dict:
    """Return ``opencli douyin detail`` for one video.

    opencli is a Node script; with nvm, ``node`` sits beside it and is often
    not on PATH, so that directory goes first.
    """
    env = {**os.environ, "PATH": f"{opencli.parent}{os.pathsep}{os.environ['PATH']}"}
    command = [str(opencli), "douyin", "detail", target, "--window", "background"]
    for attempt in range(1, attempts + 1):
        result = run(
            [*command, "-f", "json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DouyinError(
                f"opencli exited {result.returncode}: {result.stderr[-500:]}"
            ) from exc
        if envelope.get("ok"):
            data = envelope.get("data")
            data = data[0] if isinstance(data, list) and data else data
            if not isinstance(data, dict) or not data.get("aweme_id"):
                raise DouyinError(f"opencli returned no video for {target}")
            return data
        error = envelope.get("error") or {}
        # The Browser Bridge sometimes drops a command mid-flight ("Detached
        # while handling command"); the detail call only reads, so repeat it.
        if "detached" in json.dumps(error).lower() and attempt < attempts:
            continue
        message = f"opencli {error.get('code')}: {error.get('message')}"
        if error.get("code") == "AUTH_REQUIRED":
            message += " (log in to douyin.com in the Chrome profile OpenCLI uses)"
        raise DouyinError(message)
    raise AssertionError("unreachable")


def info_from_detail(detail: dict) -> dict:
    """The ``<stem>.info.json`` written beside the video."""
    aweme_id = str(detail["aweme_id"])
    created = detail.get("created")
    return {
        "platform": "douyin",
        "id": aweme_id,
        "title": detail.get("desc") or "",
        "author": detail.get("author") or "",
        "author_sec_uid": detail.get("author_sec_uid"),
        # opencli prints the post time as "YYYY-MM-DD HH:MM:SS".
        "published_at": created.replace(" ", "T") if isinstance(created, str) else None,
        "duration_seconds": detail.get("duration_s"),
        "source_url": VIDEO_URL.format(id=aweme_id),
    }


def safe_name(text: str, limit: int = 60) -> str:
    return _UNSAFE.sub(" ", text).strip()[:limit].strip()


def download(
    url: str,
    dest: Path,
    opener: Callable = urllib.request.urlopen,
) -> None:
    """Fetch ``play_url`` with Douyin's Referer into ``dest``, via a ``.part``."""
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"Referer": REFERER})
    try:
        with opener(request, timeout=60) as response, part.open("wb") as fh:
            shutil.copyfileobj(response, fh, 1 << 20)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        body = exc.read(1000).decode("utf-8", "replace")
        fields = ", ".join(f"{k} {v}" for k, v in _CDN_FIELDS.findall(body))
        raise DouyinError(
            f"video CDN answered {exc.code} ({fields or 'no detail'})"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise DouyinError(f"video download failed: {exc}") from exc
    part.replace(dest)


def fetch_video(
    settings: Settings,
    text: str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    opener: Callable = urllib.request.urlopen,
) -> Path:
    """Save the video in a share message as
    ``<recordings>/douyin-video/<author>/<title> [<id>].mp4``.
    An already downloaded, playable file is kept.

    The info file beside it is written after the video and only on a best-effort
    basis: the pipeline processes the video without it.
    """
    opencli = find_opencli(settings.douyin.opencli)
    detail = fetch_detail(extract_target(text), opencli, run)
    if not detail.get("play_url"):
        raise DouyinError(f"{detail['aweme_id']} has no video (an image post?)")
    aweme_id = str(detail["aweme_id"])
    author = safe_name(str(detail.get("author") or "")) or "unknown"
    name = safe_name(str(detail.get("desc") or "")) or aweme_id
    media = settings.recordings_dir / FOLDER / author / f"{name} [{aweme_id}].mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    if not media.exists() or recording.probe_duration(media) is None:
        download(detail["play_url"], media, opener)
    if recording.probe_duration(media) is None:
        raise DouyinError(f"downloaded file is not playable: {media}")
    try:
        media.with_suffix(".info.json").write_text(
            json.dumps(info_from_detail(detail), ensure_ascii=False, indent=2),
            "utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        log.warning("video saved without its info file: %s", exc)
    return media
