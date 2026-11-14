import json

import numpy as np

from livedigest import speakers
from livedigest.speakers import SAMPLE_RATE, UNKNOWN, unify
from livedigest.transcript import Segment

VOICES = {1.0: [1.0, 0.0, 0.0], 2.0: [0.0, 1.0, 0.0], 3.0: [0.0, 0.0, 1.0]}


def tone(*spans):
    """Samples where each (seconds, voice) span holds that voice's number."""
    return np.concatenate(
        [np.full(int(sec * SAMPLE_RATE), voice, "f4") for sec, voice in spans]
    )


def fake_embed(samples):
    return np.array(VOICES[float(samples[0])], "f4")


def run(samples, lines, registry=None):
    return unify(
        "a.ts", lines, registry, embed=fake_embed, decode=lambda path, rate: samples
    )


def test_lines_without_speakers_are_left_alone(tmp_path):
    lines = [Segment(0, 1, "你好")]
    assert unify(tmp_path / "missing.ts", lines) is lines


def test_same_voice_keeps_its_label_across_segments(tmp_path):
    registry = tmp_path / speakers.REGISTRY
    first = run(
        tone((10, 1.0), (5, 2.0)),
        [Segment(0, 10, "甲", "0:S01"), Segment(10, 15, "乙", "0:S02")],
        registry,
    )
    assert [line.speaker for line in first] == ["S01", "S02"]
    # In the next segment the model numbers the same two people the other way.
    second = run(
        tone((6, 2.0), (4, 1.0)),
        [Segment(0, 6, "乙", "0:S01"), Segment(6, 10, "甲", "0:S02")],
        registry,
    )
    assert [line.speaker for line in second] == ["S02", "S01"]
    assert json.loads(registry.read_text())["S01"]["seconds"] == 14.0


def test_longest_speaker_gets_the_lowest_number():
    lines = [Segment(0, 4, "客", "0:S01"), Segment(4, 30, "主播", "0:S02")]
    out = run(tone((4, 2.0), (26, 1.0)), lines)
    assert [line.speaker for line in out] == ["S02", "S01"]


def test_windows_of_one_file_are_matched_to_each_other():
    lines = [Segment(0, 5, "甲", "0:S01"), Segment(5, 10, "甲", "1:S01")]
    out = run(tone((10, 1.0)), lines)
    assert [line.speaker for line in out] == ["S01", "S01"]


def test_a_short_unknown_voice_is_not_registered(tmp_path):
    registry = tmp_path / speakers.REGISTRY
    lines = [Segment(0, 10, "主播", "0:S01"), Segment(10, 11, "嗯", "0:S02")]
    out = run(tone((10, 1.0), (1, 3.0)), lines, registry)
    assert [line.speaker for line in out] == ["S01", UNKNOWN]
    assert list(json.loads(registry.read_text())) == ["S01"]
    # A short remark in a known voice is still recognised.
    again = run(tone((1, 1.0)), [Segment(0, 1, "对", "0:S01")], registry)
    assert again[0].speaker == "S01"
