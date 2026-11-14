"""Put a slide broadcast's kept stills into its transcripts.

On a lecture the slide is half the content; a camera still adds little, so
only sessions classified as predominantly slides get images.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from livedigest.transcript import stamp

FRAMES = "frames"
_KEPT = re.compile(r"^p\d+_(\d+)\.jpg$")
# transcript.md lines start "[HH:MM:SS]", transcript.clean.md paragraphs "**[...]**".
_LINE_TIME = re.compile(r"^(?:\*\*)?\[(\d+):(\d{2}):(\d{2})\]")

Still = tuple[int, str]  # seconds from session start, path relative to the job


def wanted(visual: dict) -> bool:
    return visual.get("mode") == "slides"


def copy_stills(frames_dir: Path, job_dir: Path) -> list[Still]:
    """Copy kept stills not yet in ``<job>/frames`` and list them all by time."""
    target = job_dir / FRAMES
    stills = []
    for path in frames_dir.glob("p*_*.jpg"):
        if match := _KEPT.match(path.name):
            dest = target / path.name
            if not dest.exists():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            stills.append((int(match.group(1)), f"{FRAMES}/{path.name}"))
    return sorted(stills)


def embed(markdown: str, stills: list[Still]) -> str:
    """Insert each still before the first timed line at or after its time.

    Stills later than the last line follow that line, ahead of any trailing
    section such as the polished transcript's notes.
    """
    if not stills:
        return markdown
    out: list[str] = []
    pending = list(stills)
    last_timed = None
    for line in markdown.split("\n"):
        if match := _LINE_TIME.match(line):
            hours, minutes, seconds = (int(g) for g in match.groups())
            at = hours * 3600 + minutes * 60 + seconds
            while pending and pending[0][0] <= at:
                out += _image(pending.pop(0))
            out.append(line)
            last_timed = len(out)
        else:
            out.append(line)
    if pending and last_timed is not None:
        tail = [row for still in pending for row in _image(still)]
        out[last_timed:last_timed] = tail
    return "\n".join(out)


def _image(still: Still) -> list[str]:
    seconds, path = still
    return ["", f"![{stamp(seconds)}]({path})", ""]
