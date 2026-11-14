from livedigest.transcript import Segment, parse_srt, to_markdown, to_srt


def test_srt_round_trip():
    segs = [Segment(0.0, 1.5, "大家好"), Segment(3661.25, 3662.0, "第二句")]
    text = to_srt(segs)
    assert "01:01:01,250 --> 01:01:02,000" in text
    assert parse_srt(text) == segs


def test_parse_srt_joins_multiline_and_skips_empty():
    text = (
        "1\n00:00:01,000 --> 00:00:02,000\n第一行\n第二行\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n\n"
    )
    assert parse_srt(text) == [Segment(1.0, 2.0, "第一行 第二行")]


def test_markdown_has_timestamps_and_meta():
    md = to_markdown([Segment(65, 70, "你好", "说话人 1")], "标题", {"anchor": "甲"})
    assert "- anchor: 甲" in md
    assert "[00:01:05] **说话人 1** 你好" in md
