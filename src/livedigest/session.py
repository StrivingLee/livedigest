"""One live broadcast on disk: ``<root>/<platform>/<anchor>/<started>/``."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from livedigest.capture import ClosedSegment, PartPaths, read_segment_list
from livedigest.files import safe_name, write_json

STATE_LIVE = "live"
STATE_ENDED = "ended"


def iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


def epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


@dataclass
class Part:
    index: int
    started_at: str
    offset_seconds: float
    ended_at: str | None = None
    # Closed audio segments already handed on, so a restart skips them.
    segments: int = 0
    # ffmpeg survives a crash of the watcher (own process group); recovery
    # uses this to stop the orphan.
    pid: int | None = None


@dataclass(frozen=True)
class SegmentRef:
    session_dir: Path
    part: int
    index: int
    path: Path
    # Seconds from session start.
    start: float
    end: float

    @classmethod
    def of(cls, session_dir: Path, part: Part, segment: ClosedSegment) -> SegmentRef:
        return cls(
            session_dir,
            part.index,
            segment.index,
            segment.path,
            round(part.offset_seconds + segment.start, 3),
            round(part.offset_seconds + segment.end, 3),
        )


@dataclass
class Session:
    dir: Path
    id: str
    platform: str
    anchor: str
    title: str
    room_url: str
    keep: str
    started_at: str
    state: str = STATE_LIVE
    ended_at: str | None = None
    # Last time the capture was known to be running; a crash leaves this
    # behind so recovery knows how long the room has been silent.
    updated_at: str | None = None
    parts: list[Part] = field(default_factory=list)
    visual: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        root: Path,
        platform: str,
        anchor: str,
        title: str,
        room_url: str,
        keep: str,
        now: float | None = None,
    ) -> Session:
        now = time.time() if now is None else now
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H-%M-%S")
        relative = Path(platform, safe_name(anchor), stamp)
        session = cls(
            dir=root / relative,
            id=relative.as_posix(),
            platform=platform,
            anchor=anchor,
            title=title,
            room_url=room_url,
            keep=keep,
            started_at=iso(now),
            updated_at=iso(now),
        )
        session.audio_dir.mkdir(parents=True, exist_ok=True)
        session.frames_dir.mkdir(exist_ok=True)
        if keep == "video":
            session.video_dir.mkdir(exist_ok=True)
        session.save()
        return session

    @classmethod
    def load(cls, directory: Path) -> Session:
        data = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        data["parts"] = [Part(**part) for part in data.get("parts", [])]
        return cls(dir=directory, **data)

    def save(self) -> None:
        data = asdict(self)
        del data["dir"]
        write_json(self.dir / "session.json", data)

    @property
    def audio_dir(self) -> Path:
        return self.dir / "audio"

    @property
    def frames_dir(self) -> Path:
        return self.dir / "frames"

    @property
    def video_dir(self) -> Path:
        return self.dir / "video"

    @property
    def current_part(self) -> Part | None:
        return self.parts[-1] if self.parts else None

    def part(self, index: int) -> Part:
        return next(part for part in self.parts if part.index == index)

    def start_part(self, now: float | None = None) -> Part:
        now = time.time() if now is None else now
        part = Part(
            index=len(self.parts) + 1,
            started_at=iso(now),
            offset_seconds=round(epoch(iso(now)) - epoch(self.started_at), 3),
        )
        self.parts.append(part)
        self.touch(now)
        return part

    def end_part(self, now: float | None = None) -> None:
        part = self.current_part
        if part and part.ended_at is None:
            part.ended_at = iso(time.time() if now is None else now)

    def touch(self, now: float | None = None) -> None:
        self.updated_at = iso(time.time() if now is None else now)

    def last_activity(self) -> float:
        times = [epoch(self.started_at)]
        if self.updated_at:
            times.append(epoch(self.updated_at))
        times += [epoch(part.ended_at) for part in self.parts if part.ended_at]
        return max(times)

    def end(self) -> None:
        """Close the broadcast at its last sign of life, not at detection time."""
        self.end_part(self.last_activity())
        self.state = STATE_ENDED
        self.ended_at = iso(self.last_activity())


def find_sessions(root: Path, state: str | None = None) -> list[Session]:
    sessions = []
    for path in sorted(root.glob("*/*/*/session.json")):
        session = Session.load(path.parent)
        if state is None or session.state == state:
            sessions.append(session)
    return sessions


def segment_refs(session: Session) -> list[SegmentRef]:
    """Audio segments already handed on, rebuilt from the segment lists on disk.

    Only the first ``part.segments`` of each part count: anything after that
    is still open or waits for the watcher's crash recovery to hand it on.
    """
    refs = []
    for part in session.parts:
        listed = read_segment_list(PartPaths(session.dir, part.index))
        refs += [
            SegmentRef.of(session.dir, part, segment)
            for segment in listed[: part.segments]
        ]
    return refs


def transcript_path(ref: SegmentRef) -> Path:
    """Where a segment's transcript goes: ``transcripts/pNN_SSS.json``."""
    return ref.session_dir / "transcripts" / f"{ref.path.stem}.json"
