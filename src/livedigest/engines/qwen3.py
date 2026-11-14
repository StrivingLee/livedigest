"""Qwen3-ASR engine on mlx-audio, chunked so segments carry real timestamps."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from livedigest.engines.base import EngineError, EngineSettings
from livedigest.transcript import Segment

log = logging.getLogger(__name__)

LANGUAGES = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "yue": "Cantonese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
}
MAX_CHARS_PER_SECOND = 12.0
# A unit of 1 char needs many repeats (laughter is legitimate), longer units few.
_LOOP = re.compile(r"(.)\1{14,}|(.{2,20}?)\2{5,}", re.DOTALL)
# On silence or music the model tends to emit one filler per chunk ("嗯。").
_FILLERS = set("嗯啊哦呃额唉哎")
MIN_FILLER_SECONDS = 5.0
_SENTENCE = re.compile(r".*?(?:[。！？!?；;…]+|\n+|$)", re.DOTALL)
SAMPLE_RATE = 16000
# mlx-audio spends one token budget (8192) across a whole call, so a long file
# silently stops after about 50 minutes of speech. Each window is its own call.
WINDOW_SECONDS = 300.0
_CUT_SEARCH_SECONDS = 10.0


def decode_audio(path: Path, sample_rate: int = SAMPLE_RATE) -> Any:
    """Mono float32 samples, decoded by ffmpeg.

    mlx-audio picks its decoder by file extension and cannot read the MPEG-TS
    segments the capture writes, so the model gets samples instead of a path.
    """
    import numpy as np

    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-",
    ]  # fmt: skip
    try:
        result = subprocess.run(command, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise EngineError(
            f"cannot decode {path}: {exc} {detail.decode(errors='replace')[-500:]}"
        ) from exc
    if not result.stdout:
        raise EngineError(f"no audio in {path}")
    return np.frombuffer(result.stdout, dtype=np.float32)


def qwen_language(tag: str) -> str | None:
    """Map a BCP-47 tag to Qwen3-ASR's language name; None lets the model detect."""
    primary = tag.strip().lower().replace("_", "-").split("-")[0]
    return LANGUAGES.get(primary)


def collapse_loops(text: str) -> str:
    """Keep one copy of any short unit repeated in a run (ASR loop on silence)."""
    return _LOOP.sub(lambda m: m.group(1) or m.group(2), text)


def split_sentences(start: float, end: float, text: str) -> list[Segment]:
    """Split on sentence punctuation; times are proportional to character counts.

    The first start and last end are the chunk's (rounded like every time to
    the millisecond), so boundaries between chunks stay exact; only interior
    cuts are interpolated.
    """
    pieces = [p.strip() for p in _SENTENCE.findall(text) if p.strip()]
    total = sum(len(p) for p in pieces)
    if len(pieces) < 2:
        return [Segment(round(start, 3), round(end, 3), text)]
    segments, done = [], 0
    for piece in pieces:
        lo = start + (end - start) * done / total
        done += len(piece)
        hi = end if done == total else start + (end - start) * done / total
        segments.append(Segment(round(lo, 3), round(hi, 3), piece))
    return segments


def split_windows(
    samples: Any, rate: int, window: float = WINDOW_SECONDS
) -> list[tuple[float, Any]]:
    """Cut samples into ``(offset seconds, samples)`` windows of at most ``window``.

    Each cut falls on the quietest 100 ms of the window's last seconds, so it
    lands in a pause instead of inside a word.
    """
    import numpy as np

    size, search, hop = (
        int(window * rate),
        int(min(_CUT_SEARCH_SECONDS, window / 4) * rate),
        rate // 10,
    )
    windows, start = [], 0
    while len(samples) - start > size:
        tail = np.abs(samples[start + size - search : start + size])
        energy = tail[: len(tail) // hop * hop].reshape(-1, hop).mean(axis=1)
        cut = start + size - search + int(energy.argmin()) * hop + hop // 2
        windows.append((start / rate, samples[start:cut]))
        start = cut
    windows.append((start / rate, samples[start:]))
    return windows


class Qwen3Engine:
    def __init__(self, settings: EngineSettings) -> None:
        self.settings = settings
        self.name = f"qwen3-asr/{settings.qwen3_model.rsplit('/', 1)[-1]}"
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._model is None:
            try:
                from mlx_audio.stt import load
            except ImportError as exc:
                raise EngineError(f"mlx-audio is not installed: {exc}") from exc
            try:
                self._model = load(self.settings.qwen3_model)
            except (OSError, ValueError, RuntimeError) as exc:
                raise EngineError(
                    f"cannot load {self.settings.qwen3_model}: {exc}"
                ) from exc
        return self._model

    def transcribe(self, audio: Path, hotwords: Sequence[str] = ()) -> list[Segment]:
        with self._lock:
            model = self._load()
            rate = getattr(model, "sample_rate", SAMPLE_RATE)
            samples = decode_audio(audio, rate)
            segments: list[Segment] = []
            try:
                for offset, window in split_windows(samples, rate):
                    result = model.generate(
                        window,
                        language=qwen_language(self.settings.language),
                        chunk_duration=self.settings.chunk_seconds,
                        hotwords=list(hotwords) or None,
                    )
                    for raw in result.segments or []:
                        shifted = {
                            **raw,
                            "start": float(raw["start"]) + offset,
                            "end": float(raw["end"]) + offset,
                        }
                        segments += self._segments(shifted)
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                raise EngineError(f"qwen3-asr failed on {audio}: {exc}") from exc
            finally:
                _clear_cache()
        return segments

    def _segments(self, raw: dict[str, Any]) -> list[Segment]:
        start, end = float(raw["start"]), float(raw["end"])
        original = (raw.get("text") or "").strip()
        text = collapse_loops(original).strip()
        if text != original:
            log.warning("repeating output at %.1f-%.1fs collapsed", start, end)
        if not text:
            return []
        spoken = re.sub(r"[\W_]+", "", text)
        if (
            len(spoken) <= 2
            and set(spoken) <= _FILLERS
            and (end - start >= MIN_FILLER_SECONDS)
        ):
            log.warning("dropped lone filler over %.1f-%.1fs", start, end)
            return []
        if len(text) / max(end - start, 1.0) > MAX_CHARS_PER_SECOND:
            log.warning("dropped implausible output at %.1f-%.1fs", start, end)
            return []
        if self.settings.sentence_split:
            return split_sentences(start, end, text)
        return [Segment(start, end, text)]


def _clear_cache() -> None:
    """Release MLX's buffer cache; a no-op when no model ever loaded MLX."""
    mx = sys.modules.get("mlx.core")
    if mx is not None:
        mx.clear_cache()
