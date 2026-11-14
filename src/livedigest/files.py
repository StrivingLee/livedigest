"""File helpers shared by the modules that keep state on disk."""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def safe_name(text: str, limit: int = 80) -> str:
    """Names from a platform may contain path separators or control characters."""
    cleaned = _UNSAFE.sub("_", text).strip(" .")
    return cleaned[:limit] or "unknown"


def write_text(path: Path, text: str) -> None:
    """Replace ``path`` in one step, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, data: object, indent: int = 2) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


@contextmanager
def file_lock(path: Path, wait: bool = False) -> Iterator[bool]:
    """Hold an exclusive lock on ``path``; yields False when another holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def probe_duration(path: Path) -> float | None:
    """Media duration in seconds, or None when ffprobe cannot read the file."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        return float(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
