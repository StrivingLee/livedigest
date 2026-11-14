"""MOSS-Transcribe-Diarize on mlx-audio: text, sentence times and speakers.

The model numbers speakers afresh in every call, so each window's labels are
prefixed with the window (``0:S01``); ``livedigest.speakers`` turns them into
one label per voice.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from livedigest.engines.base import EngineError, EngineSettings, clear_cache
from livedigest.engines.qwen3 import (
    SAMPLE_RATE,
    collapse_loops,
    decode_audio,
    split_windows,
)
from livedigest.transcript import Segment

log = logging.getLogger(__name__)

# A 5-minute window of fast speech needs about 5,000 tokens; the library's
# default (2,048) would cut it short.
MAX_TOKENS = 8192


class MossEngine:
    def __init__(self, settings: EngineSettings) -> None:
        self.settings = settings
        self.name = f"moss/{settings.moss_model.rsplit('/', 1)[-1]}"
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._model is None:
            try:
                from mlx_audio.stt import load
            except ImportError as exc:
                raise EngineError(f"mlx-audio is not installed: {exc}") from exc
            try:
                self._model = load(self.settings.moss_model)
            except (OSError, ValueError, RuntimeError) as exc:
                raise EngineError(
                    f"cannot load {self.settings.moss_model}: {exc}"
                ) from exc
        return self._model

    def transcribe(self, audio: Path, hotwords: Sequence[str] = ()) -> list[Segment]:
        with self._lock:
            model = self._load()
            rate = getattr(model, "sample_rate", SAMPLE_RATE)
            windows = split_windows(decode_audio(audio, rate), rate)
            segments: list[Segment] = []
            try:
                for number, (offset, window) in enumerate(windows):
                    result = model.generate(
                        window, max_tokens=MAX_TOKENS, hotwords=list(hotwords) or None
                    )
                    for raw in result.segments or []:
                        segments += _segment(raw, offset, number)
            except (OSError, ValueError, RuntimeError) as exc:
                raise EngineError(f"moss failed on {audio}: {exc}") from exc
            finally:
                clear_cache()
        return segments


def _segment(raw: dict[str, Any], offset: float, window: int) -> list[Segment]:
    speaker = raw.get("speaker_id")
    text = (raw.get("text") or "").strip()
    # The library repeats the label at the start of each line.
    if speaker:
        text = text.removeprefix(f"[{speaker}]").strip()
    text = collapse_loops(text).strip()
    if not text:
        return []
    return [
        Segment(
            round(float(raw["start"]) + offset, 3),
            round(float(raw["end"]) + offset, 3),
            text,
            f"{window}:{speaker}" if speaker else None,
        )
    ]
