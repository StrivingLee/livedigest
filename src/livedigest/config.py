"""Settings from ``livedigest.toml``; secrets from ``cookies.toml`` beside it."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from livedigest.capture import CaptureSettings
from livedigest.engines import EngineSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ENV = "LIVEDIGEST_CONFIG"
COOKIES_FILE = "cookies.toml"


@dataclass
class DigestSettings:
    enabled: bool = True
    command: list[str] = field(default_factory=lambda: ["claude", "-p"])
    language: str = "简体中文"
    timeout_seconds: int = 900


@dataclass
class PolishSettings:
    enabled: bool = True
    command: list[str] = field(default_factory=lambda: ["claude", "-p"])
    # Characters of raw text sent per model call.
    chunk_chars: int = 6000
    # Rule-based paragraphs, used only when a chunk's response is unusable:
    # break at pauses longer than this or once a paragraph is this long.
    block_gap_seconds: float = 2.5
    max_block_chars: int = 300
    timeout_seconds: int = 900


@dataclass
class Settings:
    recordings_dir: Path
    output_dir: Path
    rooms_path: Path
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    transcribe: EngineSettings = field(default_factory=EngineSettings)
    digest: DigestSettings = field(default_factory=DigestSettings)
    polish: PolishSettings = field(default_factory=PolishSettings)
    glossary_dir: Path = PROJECT_ROOT / "glossary"


def _resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_settings(path: Path | None = None) -> Settings:
    """Read settings, resolving relative paths against the config file.

    Cookies are account secrets, so they live in a git-ignored
    ``cookies.toml`` (``douyin = "..."`` etc.) rather than the tracked config.
    """
    if path is None:
        env_path = os.environ.get(CONFIG_ENV)
        path = Path(env_path) if env_path else PROJECT_ROOT / "livedigest.toml"
    data = _read(path)
    base = path.resolve().parent
    cookies = {
        platform: value
        for platform, value in _read(base / COOKIES_FILE).items()
        if isinstance(value, str) and value
    }
    return Settings(
        recordings_dir=_resolve(data.get("recordings_dir", "recordings"), base),
        output_dir=_resolve(data.get("output_dir", "digests"), base),
        rooms_path=_resolve(data.get("rooms_path", "rooms.toml"), base),
        capture=CaptureSettings(**data.get("capture", {}), cookies=cookies),
        transcribe=EngineSettings(**data.get("transcribe", {})),
        digest=DigestSettings(**data.get("digest", {})),
        polish=PolishSettings(**data.get("polish", {})),
        glossary_dir=_resolve(data.get("glossary_dir", "glossary"), base),
    )
