"""Timed transcript model and its SRT, Markdown, and JSON forms."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from livedigest.files import write_json


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


def _clock(seconds: float, sep: str = ",") -> str:
    millis = round(max(seconds, 0) * 1000)
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def stamp(seconds: float) -> str:
    """``HH:MM:SS`` offset used in Markdown and digests."""
    return _clock(seconds)[:8]


def to_srt(segments: list[Segment]) -> str:
    blocks = []
    for index, seg in enumerate(segments, start=1):
        text = f"{seg.speaker}: {seg.text}" if seg.speaker else seg.text
        blocks.append(f"{index}\n{_clock(seg.start)} --> {_clock(seg.end)}\n{text}\n")
    return "\n".join(blocks)


META_KEYS = ("anchor", "source_url", "started_at", "duration")


def meta_lines(meta: dict, keys: tuple[str, ...] = META_KEYS) -> list[str]:
    """The broadcast's facts as Markdown bullets, skipping unknown ones."""
    return [f"- {key}: {meta[key]}" for key in keys if meta.get(key)]


def to_markdown(segments: list[Segment], title: str, meta: dict) -> str:
    """One timestamped line per segment, under a short metadata header."""
    header = [f"# {title}", "", *meta_lines(meta, (*META_KEYS, "engine")), ""]
    body = []
    for seg in segments:
        who = f"**{seg.speaker}** " if seg.speaker else ""
        body.append(f"[{stamp(seg.start)}] {who}{seg.text}")
    return "\n".join(header + body) + "\n"


def compact_lines(segments: list[Segment]) -> str:
    """Timestamped plain lines for prompting a language model."""
    lines = []
    for seg in segments:
        who = f"{seg.speaker}: " if seg.speaker else ""
        lines.append(f"[{stamp(seg.start)}] {who}{seg.text}")
    return "\n".join(lines)


def save_json(segments: list[Segment], path: Path) -> None:
    write_json(path, [asdict(seg) for seg in segments], indent=1)


def load_json(path: Path) -> list[Segment]:
    return [Segment(**item) for item in json.loads(path.read_text("utf-8"))]
