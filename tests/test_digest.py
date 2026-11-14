from livedigest import digest
from livedigest.transcript import Segment


def test_prompt_names_the_recording_platform():
    segments = [Segment(0, 1, "大家好")]
    meta = {"anchor": "主播甲", "kind": "live", "platform": "抖音直播"}
    prompt = digest.build_prompt(segments, meta, "简体中文")
    assert "一场抖音直播的自动语音转写稿" in prompt
    assert "小红书" not in prompt
    assert "这场直播" in prompt


def test_prompt_without_platform_stays_neutral():
    prompt = digest.build_prompt([Segment(0, 1, "大家好")], {}, "简体中文")
    assert "一场直播的自动语音转写稿" in prompt
