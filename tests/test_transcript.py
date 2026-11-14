from livedigest.transcript import Segment, to_markdown, to_srt


def test_srt_times():
    segs = [Segment(0.0, 1.5, "大家好"), Segment(3661.25, 3662.0, "第二句")]
    assert "01:01:01,250 --> 01:01:02,000" in to_srt(segs)


def test_markdown_has_timestamps_and_meta():
    md = to_markdown([Segment(65, 70, "你好", "说话人 1")], "标题", {"anchor": "甲"})
    assert "- anchor: 甲" in md
    assert "[00:01:05] **说话人 1** 你好" in md
