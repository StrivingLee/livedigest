"""Decide which stills are worth keeping.

Measured on real streams, stills 10 s apart differ by a median Hamming
distance of 11/256 on a slides lecture (85% of pairs <= 16, a jump > 40 at
each new slide), 51 on a talking-head camera and 85 on an auction camera.
So a window where most consecutive stills barely change is a slide or screen
share: keep each distinct slide. Anything else is a camera: one still every
few minutes is enough context.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from livedigest.capture import RAW_FRAME_RE
from livedigest.session import Session

log = logging.getLogger(__name__)

HASH_WIDTH = 17
HASH_HEIGHT = 16
STILL_DISTANCE = 16  # consecutive stills this close count as "unchanged"
SLIDES_SHARE = 0.5  # share of unchanged pairs that marks a window as slides
NEW_SLIDE_DISTANCE = 32  # a slide is new when this far from the last kept one
CAMERA_KEEP_SECONDS = 300  # one context still per this many seconds on camera
DEFAULT_INTERVAL = 10


def hash_gray(pixels: bytes, width: int = HASH_WIDTH, height: int = HASH_HEIGHT) -> int:
    """Difference hash: one bit per horizontally adjacent pixel pair."""
    if len(pixels) != width * height:
        raise ValueError(f"expected {width * height} bytes, got {len(pixels)}")
    value = 0
    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        for x in range(width - 1):
            value = (value << 1) | (row[x] > row[x + 1])
    return value


def dhash(path: Path) -> int | None:
    """None when the image cannot be decoded (e.g. a truncated write)."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", str(path),
                "-vf", f"scale={HASH_WIDTH}:{HASH_HEIGHT}:flags=area,format=gray",
                "-frames:v", "1", "-f", "rawvideo", "-",
            ],
            capture_output=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return hash_gray(result.stdout)
    except ValueError:
        return None


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def classify(distances: list[int]) -> tuple[str, float]:
    """Window mode and the share of near-identical consecutive pairs."""
    if not distances:
        return "camera", 0.0
    share = sum(d <= STILL_DISTANCE for d in distances) / len(distances)
    return ("slides" if share >= SLIDES_SHARE else "camera"), share


@dataclass
class WindowStats:
    part: int
    mode: str
    still_share: float
    frames: int
    kept: list[Path] = field(default_factory=list)
    # Seconds from session start covered by the window's stills.
    start: float | None = None
    end: float | None = None


def frame_time(path: Path, offset: float, interval: int) -> float:
    match = RAW_FRAME_RE.match(path.name)
    if not match:
        raise ValueError(f"not a raw frame name: {path.name}")
    return offset + int(match.group(2)) * interval


def process_window(
    session: Session,
    part: int,
    new_frame_paths: list[Path],
    interval: int = DEFAULT_INTERVAL,
) -> WindowStats:
    """Keep, rename or delete one window of raw stills and record the outcome.

    Hashes of the last still and the last kept still carry over in
    ``session.visual`` so decisions span window boundaries.
    """
    visual = session.visual
    offset = session.part(part).offset_seconds
    timed = sorted(
        (frame_time(path, offset, interval), path) for path in new_frame_paths
    )
    hashes = []
    for when, path in timed:
        value = dhash(path)
        if value is None:
            log.warning("unreadable still %s dropped", path.name)
            path.unlink(missing_ok=True)
        else:
            hashes.append((when, path, value))

    previous = visual.get("last_hash")
    chain = ([int(previous, 16)] if previous else []) + [h for _, _, h in hashes]
    distances = [distance(a, b) for a, b in zip(chain, chain[1:], strict=False)]
    mode, share = classify(distances)
    if len(distances) < 2 and visual.get("windows"):
        # Too few stills to judge; assume the broadcast has not changed style.
        mode = visual["windows"][-1]["mode"]

    last_kept = visual.get("last_kept_hash")
    last_kept_hash = int(last_kept, 16) if last_kept else None
    last_kept_time = visual.get("last_kept_time")
    stats = WindowStats(part, mode, round(share, 3), len(hashes))
    for when, path, value in hashes:
        if mode == "slides":
            keep = (
                last_kept_hash is None
                or distance(value, last_kept_hash) > NEW_SLIDE_DISTANCE
            )
        else:
            keep = (
                last_kept_time is None or when - last_kept_time >= CAMERA_KEEP_SECONDS
            )
        if keep:
            target = path.with_name(f"p{part:02d}_{round(when):06d}.jpg")
            path.replace(target)
            stats.kept.append(target)
            last_kept_hash, last_kept_time = value, when
        else:
            path.unlink(missing_ok=True)
    if hashes:
        stats.start, stats.end = hashes[0][0], hashes[-1][0]
        visual["last_hash"] = f"{hashes[-1][2]:064x}"
    if last_kept_hash is not None:
        visual["last_kept_hash"] = f"{last_kept_hash:064x}"
        visual["last_kept_time"] = last_kept_time
    _record(visual, stats)
    session.save()
    return stats


def _record(visual: dict, stats: WindowStats) -> None:
    visual.setdefault("windows", []).append(
        {
            "part": stats.part,
            "start": stats.start,
            "end": stats.end,
            "mode": stats.mode,
            "still_share": stats.still_share,
            "frames": stats.frames,
            "kept": len(stats.kept),
        }
    )
    windows = visual["windows"]
    visual["kept_frames"] = sum(w["kept"] for w in windows)
    visual["seen_frames"] = sum(w["frames"] for w in windows)
    judged = [w for w in windows if w["frames"]]
    slides = sum(w["mode"] == "slides" for w in judged)
    visual["slides_windows"] = slides
    visual["camera_windows"] = len(judged) - slides
    visual["mode"] = (
        "none" if not judged else "slides" if slides * 2 >= len(judged) else "camera"
    )
