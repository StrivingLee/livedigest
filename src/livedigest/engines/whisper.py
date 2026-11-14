"""mlx-whisper engine (fallback)."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from livedigest.engines.base import EngineError, EngineSettings
from livedigest.transcript import Segment

log = logging.getLogger(__name__)


class WhisperEngine:
    def __init__(self, settings: EngineSettings) -> None:
        self.settings = settings
        self.name = f"mlx-whisper/{settings.whisper_model.rsplit('/', 1)[-1]}"
        self._lock = threading.Lock()

    def _prompt(self, hotwords: Sequence[str]) -> str | None:
        prompt = self.settings.whisper_prompt
        if hotwords:
            # Whisper imitates the prompt's punctuation, so phrase the terms as
            # an ordinary sentence. It keeps only the last ~224 prompt tokens.
            prompt = f"{prompt}直播中会提到{'、'.join(hotwords)}。"[:150]
        return prompt or None

    def transcribe(self, audio: Path, hotwords: Sequence[str] = ()) -> list[Segment]:
        try:
            import mlx_whisper
        except ImportError as exc:
            raise EngineError(f"mlx-whisper is not installed: {exc}") from exc
        language = self.settings.language
        with self._lock:
            try:
                result = mlx_whisper.transcribe(
                    str(audio),
                    path_or_hf_repo=self.settings.whisper_model,
                    language=None if language == "auto" else language.split("-")[0],
                    initial_prompt=self._prompt(hotwords),
                    # Carrying text across windows makes broadcasts loop.
                    condition_on_previous_text=False,
                    verbose=None,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise EngineError(f"mlx-whisper failed on {audio}: {exc}") from exc
            finally:
                _clear_cache()
        return [
            Segment(
                start=float(s["start"]), end=float(s["end"]), text=s["text"].strip()
            )
            for s in result["segments"]
            if s["text"].strip() and not _is_silence(s)
        ]


def _is_silence(segment: dict) -> bool:
    """Whisper's own no-speech rule; it otherwise invents sign-off lines."""
    return (
        segment.get("no_speech_prob", 0.0) > 0.6
        and segment.get("avg_logprob", 0.0) < -1.0
    )


def _clear_cache() -> None:
    """Release MLX's buffer cache; a no-op when no model ever loaded MLX."""
    mx = sys.modules.get("mlx.core")
    if mx is not None:
        mx.clear_cache()
