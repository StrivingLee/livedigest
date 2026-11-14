"""Timed transcript model and its SRT, Markdown, and JSON forms."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


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


_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
)


def parse_srt(text: str) -> list[Segment]:
    segments = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if not match:
                continue
            h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in match.groups())
            body = " ".join(part.strip() for part in lines[i + 1 :]).strip()
            if body:
                segments.append(
                    Segment(
                        start=h1 * 3600 + m1 * 60 + s1 + ms1 / 1000,
                        end=h2 * 3600 + m2 * 60 + s2 + ms2 / 1000,
                        text=body,
                    )
                )
            break
    return segments


def to_markdown(segments: list[Segment], title: str, meta: dict) -> str:
    """One timestamped line per segment, under a short metadata header."""
    header = [f"# {title}", ""]
    for key in ("anchor", "source_url", "started_at", "duration", "engine"):
        if meta.get(key):
            header.append(f"- {key}: {meta[key]}")
    header.append("")
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
    payload = [asdict(seg) for seg in segments]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")


def load_json(path: Path) -> list[Segment]:
    return [Segment(**item) for item in json.loads(path.read_text("utf-8"))]
