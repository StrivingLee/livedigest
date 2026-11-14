"""Speech-to-text engines; models load lazily and once per process."""

from __future__ import annotations

import importlib.util
from functools import cache

from livedigest.engines.base import Engine, EngineError, EngineSettings

__all__ = ["Engine", "EngineError", "EngineSettings", "get_engine"]


def get_engine(settings: EngineSettings) -> Engine:
    """Return the engine for ``settings``, built once and shared afterwards."""
    if settings.engine not in {"auto", "qwen3", "whisper"}:
        raise EngineError(f"unknown speech-to-text engine: {settings.engine}")
    return _build(settings)


@cache
def _build(settings: EngineSettings) -> Engine:
    use_qwen = settings.engine == "qwen3" or (
        settings.engine == "auto" and importlib.util.find_spec("mlx_audio")
    )
    if use_qwen:
        from livedigest.engines.qwen3 import Qwen3Engine

        return Qwen3Engine(settings)
    from livedigest.engines.whisper import WhisperEngine

    return WhisperEngine(settings)
