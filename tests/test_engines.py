import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from livedigest import engines
from livedigest.engines import EngineError, EngineSettings, get_engine, qwen3
from livedigest.engines.qwen3 import (
    Qwen3Engine,
    collapse_loops,
    qwen_language,
    split_sentences,
)
from livedigest.engines.whisper import WhisperEngine
from livedigest.transcript import Segment


class FakeModel:
    def __init__(self, segments=None, error=None):
        self.segments = segments or []
        self.error = error
        self.calls = []

    def generate(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(segments=self.segments)


def qwen_with(monkeypatch, model, **settings):
    loads = []

    def load(name):
        loads.append(name)
        return model

    monkeypatch.setattr(qwen3, "decode_audio", lambda path, rate: np.zeros(1600, "f4"))
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = load
    monkeypatch.setitem(sys.modules, "mlx_audio", types.ModuleType("mlx_audio"))
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    return Qwen3Engine(EngineSettings(**settings)), loads


def raw(start, end, text):
    return {"start": start, "end": end, "text": text, "language": "Chinese"}


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("zh", "Chinese"),
        ("zh-Hans", "Chinese"),
        ("zh_CN", "Chinese"),
        ("en", "English"),
        ("en-US", "English"),
        ("auto", None),
        ("xx", None),
    ],
)
def test_language_mapping(tag, expected):
    assert qwen_language(tag) == expected


def test_qwen_passes_language_chunks_and_hotwords(monkeypatch):
    model = FakeModel([raw(0, 20, "你好")])
    engine, loads = qwen_with(monkeypatch, model, chunk_seconds=15.0)
    engine.transcribe(Path("a.m4a"), ["苹果", "华为"])
    engine.transcribe(Path("b.m4a"))
    assert loads == ["mlx-community/Qwen3-ASR-1.7B-8bit"]  # loaded once
    assert model.calls[0][1] == {
        "language": "Chinese",
        "chunk_duration": 15.0,
        "hotwords": ["苹果", "华为"],
    }
    assert model.calls[1][1]["hotwords"] is None
    assert engine.name == "qwen3-asr/Qwen3-ASR-1.7B-8bit"


def test_empty_segments_dropped(monkeypatch):
    model = FakeModel([raw(0, 20, "  "), raw(20, 40, "好的。")])
    engine, _ = qwen_with(monkeypatch, model)
    assert engine.transcribe(Path("a.m4a")) == [Segment(20, 40, "好的。")]


def test_looping_output_is_collapsed(monkeypatch):
    looped = "谢谢大家" + "谢谢观看" * 30
    model = FakeModel([raw(0, 20, looped)])
    engine, _ = qwen_with(monkeypatch, model, sentence_split=False)
    assert engine.transcribe(Path("a.m4a")) == [Segment(0, 20, "谢谢大家谢谢观看")]
    assert collapse_loops("哈哈哈哈") == "哈哈哈哈"  # real laughter stays


def test_implausibly_dense_output_is_dropped(monkeypatch):
    dense = "".join(chr(0x4E00 + i) for i in range(400))  # 20 chars/s, no loop
    engine, _ = qwen_with(monkeypatch, FakeModel([raw(0, 20, dense)]))
    assert engine.transcribe(Path("a.m4a")) == []


def test_sentence_split_keeps_chunk_edges():
    segments = split_sentences(10.0, 30.0, "大家好。欢迎来到直播间！今天讲手表")
    assert [s.text for s in segments] == ["大家好。", "欢迎来到直播间！", "今天讲手表"]
    assert segments[0].start == 10.0 and segments[-1].end == 30.0
    assert all(a.end == b.start for a, b in zip(segments, segments[1:], strict=False))
    assert split_sentences(0, 5, "没有标点") == [Segment(0, 5, "没有标点")]
    [single] = split_sentences(1.23456, 4.56789, "一句话。")
    assert (single.start, single.end) == (1.235, 4.568)
    pair = split_sentences(1.23456, 4.56789, "一。二。")
    assert (pair[0].start, pair[-1].end) == (1.235, 4.568)


def test_sentence_split_setting(monkeypatch):
    model = FakeModel([raw(0, 20, "你好。再见。")])
    on, _ = qwen_with(monkeypatch, model)
    off, _ = qwen_with(monkeypatch, model, sentence_split=False)
    assert len(on.transcribe(Path("a.m4a"))) == 2
    assert len(off.transcribe(Path("a.m4a"))) == 1


def test_qwen_gets_samples_decoded_from_mpeg_ts(tmp_path):
    segment = tmp_path / "p01_000.ts"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2",
            "-c:a", "aac", "-f", "mpegts", str(segment),
        ],
        check=True,
    )  # fmt: skip
    samples = qwen3.decode_audio(segment)
    assert samples.dtype.name == "float32"
    assert abs(len(samples) / qwen3.SAMPLE_RATE - 2.0) < 0.1
    with pytest.raises(EngineError, match="cannot decode"):
        qwen3.decode_audio(tmp_path / "missing.ts")


def test_decode_failure_becomes_engine_error(monkeypatch):
    engine, _ = qwen_with(monkeypatch, FakeModel(error=ValueError("bad audio")))
    with pytest.raises(EngineError, match="bad audio"):
        engine.transcribe(Path("broken.m4a"))


def test_get_engine_is_cached_per_settings():
    first = get_engine(EngineSettings(engine="qwen3"))
    assert get_engine(EngineSettings(engine="qwen3")) is first
    assert get_engine(EngineSettings(engine="qwen3", chunk_seconds=10)) is not first
    with pytest.raises(EngineError):
        get_engine(EngineSettings(engine="nope"))


def test_auto_prefers_qwen_and_falls_back_to_whisper(monkeypatch):
    settings = EngineSettings()
    monkeypatch.setattr(engines.importlib.util, "find_spec", lambda name: object())
    engines._build.cache_clear()
    assert isinstance(get_engine(settings), Qwen3Engine)
    monkeypatch.setattr(engines.importlib.util, "find_spec", lambda name: None)
    engines._build.cache_clear()
    assert isinstance(get_engine(settings), WhisperEngine)
    engines._build.cache_clear()


def test_whisper_prompt_and_language(monkeypatch):
    seen = {}

    def transcribe(path, **kwargs):
        seen.update(kwargs)
        return {
            "segments": [
                {"start": 0, "end": 2, "text": " 你好 "},
                {"start": 2, "end": 3, "text": " "},
            ]
        }

    monkeypatch.setitem(
        sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe)
    )
    engine = WhisperEngine(EngineSettings(language="zh-Hans"))
    assert engine.transcribe(Path("a.m4a"), ["苹果", "华为"]) == [Segment(0, 2, "你好")]
    assert seen["language"] == "zh"
    assert seen["condition_on_previous_text"] is False
    assert seen["initial_prompt"].endswith("直播中会提到苹果、华为。")
    assert len(seen["initial_prompt"]) <= 150
    assert engine.name == "mlx-whisper/whisper-large-v3-turbo"


def test_lone_filler_over_long_chunk_is_dropped(monkeypatch):
    model = FakeModel(
        [raw(0, 15, "嗯。"), raw(15, 17, "嗯。"), raw(17, 30, "嗯，好。")]
    )
    engine, _ = qwen_with(monkeypatch, model)
    texts = [s.text for s in engine.transcribe(Path("a.m4a"))]
    assert texts == ["嗯。", "嗯，好。"]  # short or real speech is kept


def test_whisper_drops_no_speech_segments(monkeypatch):
    segments = [
        {
            "start": 0,
            "end": 30,
            "text": "请订阅",
            "no_speech_prob": 0.9,
            "avg_logprob": -1.5,
        },
        {
            "start": 30,
            "end": 32,
            "text": "你好",
            "no_speech_prob": 0.1,
            "avg_logprob": -0.3,
        },
    ]
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=lambda path, **kw: {"segments": segments}),
    )
    engine = WhisperEngine(EngineSettings())
    assert engine.transcribe(Path("a.m4a")) == [Segment(30, 32, "你好")]


def test_split_windows_cuts_in_the_quiet_part():
    rate = 100
    samples = np.ones(50 * rate, "f4")
    samples[1700:1720] = 0.0  # a pause 17 s in, inside the first window's tail
    windows = qwen3.split_windows(samples, rate, window=20.0)
    assert [round(offset, 1) for offset, _ in windows] == [0.0, 17.1, 32.1]
    assert sum(len(chunk) for _, chunk in windows) == len(samples)


def test_qwen_long_audio_runs_one_call_per_window(monkeypatch):
    model = FakeModel([raw(0, 20, "你好")])
    engine, _ = qwen_with(monkeypatch, model)
    monkeypatch.setattr(
        qwen3, "split_windows", lambda samples, rate: [(0.0, "a"), (300.5, "b")]
    )
    segments = engine.transcribe(Path("long.mp4"))
    assert [call[0] for call in model.calls] == ["a", "b"]
    assert [(s.start, s.end) for s in segments] == [(0.0, 20.0), (300.5, 320.5)]
