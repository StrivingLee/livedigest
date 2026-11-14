"""Shared types for the speech-to-text engines."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from livedigest.transcript import Segment


class EngineError(Exception):
    """An engine could not load its model or transcribe a file."""


@dataclass(frozen=True)
class EngineSettings:
    """Engine choice and tuning; hashable so engines can be cached by it."""

    # "auto" | "qwen3" | "whisper" | "moss" (slower, labels speakers)
    engine: str = "auto"
    language: str = "zh"  # BCP-47 tag, or "auto" to detect
    qwen3_model: str = "mlx-community/Qwen3-ASR-1.7B-8bit"
    # Qwen3-ASR sees one chunk at a time; with the library default (20 min)
    # a whole segment would come back as a single timestamp.
    chunk_seconds: float = 20.0
    sentence_split: bool = True
    moss_model: str = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    whisper_prompt: str = "以下是普通话的直播内容，请使用简体中文。"


class Engine(Protocol):
    name: str

    def transcribe(self, audio: Path, hotwords: Sequence[str] = ()) -> list[Segment]:
        """Transcribe one file; times are seconds from the start of that file."""
        ...


def clear_cache() -> None:
    """Release MLX's buffer cache; a no-op when no model ever loaded MLX."""
    mx = sys.modules.get("mlx.core")
    if mx is not None:
        mx.clear_cache()
