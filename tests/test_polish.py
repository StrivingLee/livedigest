import sys

from conftest import FAKE_POLISHER

from livedigest import polish
from livedigest.config import PolishSettings
from livedigest.transcript import Segment


def test_clean_segments_drops_hallucinations_and_repeats():
    segs = [
        Segment(0, 1, "大家好"),
        Segment(1, 2, "谢谢观看！"),
        Segment(2, 3, "好的"),
        Segment(3, 4, "好的。"),
        Segment(4, 5, "  "),
    ]
    out = polish.clean_segments(segs)
    assert [(s.text, s.end) for s in out] == [("大家好", 1), ("好的", 4)]


def test_make_blocks_breaks_on_pause_and_length():
    segs = [
        Segment(0, 1, "第一句"),
        Segment(1.2, 2, "第二句。"),
        Segment(10, 11, "停顿之后" + "x" * 8),
        Segment(11, 12, "还没说完"),
        Segment(12, 13, "说完了吗"),
        Segment(13, 14, "下一段"),
    ]
    blocks = polish.make_blocks(segs, max_chars=12, gap_seconds=2.5)
    # Over the limit after "停顿之后xxxxxxxx", but it waits for a clause end.
    assert [(b.start, b.raw) for b in blocks] == [
        (0, "第一句，第二句。"),
        (10, "停顿之后" + "x" * 8 + "，还没说完，说完了吗"),
        (13, "下一段"),
    ]


def test_make_blocks_hard_cap_without_clause_end():
    segs = [Segment(i, i + 1, "一二三四五") for i in range(5)]
    blocks = polish.make_blocks(segs, max_chars=10, gap_seconds=2.5)
    assert [len(b.raw.replace("，", "")) for b in blocks] == [15, 10]


def test_apply_rewrite_guards():
    raw = "就是那个我觉得这个价格是一百九十九元然后呢另外一款是320元对吧就是这样子" * 2
    ok = polish.Block(1, 0, 1, raw[: len(raw) // 2])
    polish.apply_rewrite(
        ok, {"text": "一款 199 元，另一款 320 元。", "unsure": ["x→y"]}
    )
    assert ok.text.startswith("一款") and ok.unsure == ["x→y"]
    assert ok.warning is None

    longer = polish.Block(2, 0, 1, "短句")
    polish.apply_rewrite(longer, {"text": "这是一段原文里完全没有的很长的补充说明" * 3})
    assert longer.text == "短句" and "longer" in longer.warning

    gutted = polish.Block(3, 0, 1, raw)
    polish.apply_rewrite(gutted, {"text": "好"})
    assert gutted.text == raw and "removed too much" in gutted.warning

    numbers = polish.Block(4, 0, 1, "价格是320元")
    polish.apply_rewrite(numbers, {"text": "价格是三百二十元"})
    assert numbers.text == "价格是三百二十元" and "320" in numbers.warning

    missing = polish.Block(5, 0, 1, "原文")
    polish.apply_rewrite(missing, None)
    assert missing.text == "原文" and missing.warning


def test_parse_response_ignores_surrounding_prose():
    text = '好的：\n```json\n[{"id": 1, "text": "a"}]\n```'
    assert polish.parse_response(text) == [{"id": 1, "text": "a"}]


def test_polish_end_to_end_with_fake_model():
    settings = PolishSettings(command=[sys.executable, str(FAKE_POLISHER)])
    segs = [Segment(0, 1, "就是大家好"), Segment(5, 6, "那个今天聊股票")]
    blocks = polish.polish(segs, "甲", ["股票"], settings)
    assert [b.text for b in blocks] == ["大家好", "今天聊股票"]
    md = polish.to_markdown(blocks, "标题", {})
    assert "**[00:00:05]** 今天聊股票" in md


def test_polish_keeps_raw_when_some_chunks_fail(tmp_path):
    flaky = tmp_path / "flaky.py"
    flaky.write_text(
        "import json, sys\n"
        "p = sys.stdin.read()\n"
        "b = json.loads(p.split('<<<BLOCKS')[1].split('BLOCKS>>>')[0])\n"
        "if '坏' in p: sys.exit(1)\n"
        "print(json.dumps([{'from': x['id'], 'text': x['text'] + '!'} for x in b]))\n"
    )
    settings = PolishSettings(command=[sys.executable, str(flaky)], chunk_chars=3)
    segs = [Segment(0, 1, "一二三。"), Segment(9, 10, "坏的")]
    blocks = polish.polish(segs, "甲", [], settings)
    assert blocks[0].text == "一二三。!"
    assert blocks[1].text == "坏的" and "chunk failed" in blocks[1].warning


def test_assemble_maps_paragraphs_to_segment_spans():
    numbered = list(
        enumerate(
            [
                Segment(0, 2, "嗯"),
                Segment(2, 5, "就是今天聊三件事"),
                Segment(5, 8, "第一件是防晒"),
                Segment(8, 9, "几点起床"),
                Segment(9, 12, "7点"),
            ],
            start=10,
        )
    )
    items = [
        {"from": 11, "text": "今天聊三件事，第一件是防晒。"},
        {"from": 13, "text": "【问】几点起床？7 点。", "unsure": ["几点→几点"]},
    ]
    blocks = polish.assemble(numbered, items)
    # The first paragraph also owns the dropped "嗯" but is stamped where its
    # content starts.
    assert [(b.id, b.start, b.end) for b in blocks] == [(11, 2, 8), (13, 8, 12)]
    assert blocks[0].raw == "嗯，就是今天聊三件事，第一件是防晒"
    assert blocks[1].unsure == ["几点→几点"]
    assert blocks[1].warning is None


def test_assemble_rejects_bad_ids():
    numbered = list(enumerate([Segment(0, 1, "一"), Segment(1, 2, "二")], start=1))
    for items in (
        [],
        [{"from": 1, "text": "a"}, {"from": 9, "text": "b"}],
        [{"from": 2, "text": "a"}, {"from": 1, "text": "b"}],
        [{"from": 1, "text": "a"}, {"from": 1, "text": "b"}],
    ):
        try:
            polish.assemble(numbered, items)
        except ValueError:
            continue
        raise AssertionError(f"accepted {items}")


def test_polish_raises_when_everything_fails():
    settings = PolishSettings(command=["false"])
    try:
        polish.polish([Segment(0, 1, "一")], "甲", [], settings)
    except polish.PolishError:
        return
    raise AssertionError("expected PolishError")


def test_markdown_puts_questions_in_own_paragraphs():
    block = polish.Block(1, 65, 70, "raw", text="开场白。【问】几点起床？7 点。")
    md = polish.to_markdown([block], "标题", {})
    assert "**[00:01:05]** 开场白。\n\n**问：**几点起床？7 点。\n" in md
    only_q = polish.Block(2, 0, 1, "raw", text="【问】好吗？好。")
    assert "**[00:00:00]** **问：**好吗？好。" in polish.to_markdown([only_q], "t", {})


def test_markdown_splits_model_paragraph_breaks():
    block = polish.Block(1, 0, 1, "raw", text="第一段。\n\n第二段。【问】问？答。")
    md = polish.to_markdown([block], "t", {})
    assert "**[00:00:00]** 第一段。\n\n第二段。\n\n**问：**问？答。\n" in md


def test_prompt_names_the_recording_platform():
    numbered = [(1, Segment(0, 1, "大家好"))]
    douyin = polish.build_prompt(numbered, "主播甲", [], "live", "抖音直播")
    assert "下面是一场抖音直播" in douyin
    assert "小红书" not in douyin
    video = polish.build_prompt(numbered, "主播甲", [], "video", "douyin-video")
    assert "下面是一条抖音短视频" in video
