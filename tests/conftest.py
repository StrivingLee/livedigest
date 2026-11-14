import subprocess
import sys
from pathlib import Path

import pytest

from livedigest.config import DigestSettings, PolishSettings, Settings

FAKE_POLISHER = Path(__file__).parent / "fake_polisher.py"


def make_media(path: Path, seconds: float = 2.0) -> Path:
    """Write a short silent-video-plus-tone MP4 (or audio file by suffix)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if path.suffix in {".mp4", ".ts"}:
        inputs += ["-f", "lavfi", "-i", f"color=c=black:s=64x64:d={seconds}"]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *inputs, "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        recordings_dir=tmp_path / "recordings",
        output_dir=tmp_path / "digests",
        rooms_path=tmp_path / "rooms.toml",
        digest=DigestSettings(command=["cat"]),
        polish=PolishSettings(command=[sys.executable, str(FAKE_POLISHER)]),
        glossary_dir=tmp_path / "glossary",
    )
