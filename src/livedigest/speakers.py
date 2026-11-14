"""Keep one label per voice across the audio segments of a broadcast.

A diarizing engine numbers speakers afresh in every call, so its ``S01`` in
one five-minute segment need not be its ``S01`` in the next. Here each
speaker of a call is reduced to a voice embedding (ECAPA-TDNN, 192 numbers)
and compared with the voices already met: the same person scores about 0.9,
different people below 0.6. The voices met so far are kept in a small JSON
file per broadcast, so labels also survive a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from livedigest.engines.qwen3 import SAMPLE_RATE, decode_audio
from livedigest.files import read_json, write_json
from livedigest.transcript import Segment

log = logging.getLogger(__name__)

MATCH = 0.7  # cosine similarity at or above which two voices are one person
# Shorter speech gives an unreliable embedding: it may match a known voice
# but never founds a new one.
MIN_SECONDS = 3.0
MAX_SECONDS = 60.0  # speech per speaker used for the embedding
UNKNOWN = "S?"
REGISTRY = "speakers.json"  # in the broadcast's folder


def embed(samples: Any) -> Any:
    """A unit-length voice embedding of mono 16 kHz speech."""
    import numpy as np
    from mlx_audio.codec.models.ecapa_tdnn.speaker import (
        EcapaSpeakerEmbeddingExtractor,
    )

    global _extractor
    if _extractor is None:
        _extractor = EcapaSpeakerEmbeddingExtractor.from_pretrained()
    return np.array(_extractor.encode(samples), dtype=np.float32)


_extractor: Any = None


def unify(
    audio: Path,
    lines: list[Segment],
    registry: Path | None = None,
    *,
    embed: Callable[[Any], Any] = embed,
    decode: Callable[[Path, int], Any] = decode_audio,
) -> list[Segment]:
    """Replace each call-local speaker label with the broadcast-wide one.

    ``lines`` carry times from the start of ``audio``. ``registry`` is the
    broadcast's file of known voices; without one, labels are consistent
    within this call only. Lines without a speaker are returned unchanged.
    """
    import numpy as np

    groups: dict[str, list[Segment]] = {}
    for line in lines:
        if line.speaker:
            groups.setdefault(line.speaker, []).append(line)
    if not groups:
        return lines
    samples = decode(audio, SAMPLE_RATE)
    stored = read_json(registry) if registry else {}
    voices = {
        name: (np.array(voice["embedding"], dtype=np.float32), voice["seconds"])
        for name, voice in stored.items()
    }
    spoken = {
        local: np.concatenate(
            [
                samples[int(line.start * SAMPLE_RATE) : int(line.end * SAMPLE_RATE)]
                for line in items
            ]
        )[: int(MAX_SECONDS * SAMPLE_RATE)]
        for local, items in groups.items()
    }
    labels: dict[str, str] = {}
    # Longest first, so the main speakers take the low numbers.
    for local in sorted(spoken, key=lambda key: -len(spoken[key])):
        seconds = len(spoken[local]) / SAMPLE_RATE
        if seconds < 0.5:
            labels[local] = UNKNOWN
            continue
        vector = embed(spoken[local])
        scores = {name: float(vector @ known) for name, (known, _) in voices.items()}
        best = max(scores, key=scores.get, default=None)
        if best is not None and scores[best] >= MATCH:
            known, total = voices[best]
            mean = known * total + vector * seconds
            voices[best] = (mean / np.linalg.norm(mean), total + seconds)
            labels[local] = best
        elif seconds >= MIN_SECONDS:
            labels[local] = f"S{len(voices) + 1:02d}"
            voices[labels[local]] = (vector, seconds)
        else:
            labels[local] = UNKNOWN
    if registry:
        registry.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            registry,
            {
                name: {
                    "seconds": round(seconds, 1),
                    "embedding": [round(float(x), 5) for x in vector],
                }
                for name, (vector, seconds) in voices.items()
            },
            indent=None,
        )
    return [
        replace(line, speaker=labels[line.speaker]) if line.speaker else line
        for line in lines
    ]
