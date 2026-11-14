"""Turn raw ASR segments into a clean reading transcript.

Two passes:

1. Rules: drop known Whisper hallucinations and repeated loops.
2. An LLM receives the segments with ids and returns paragraphs of plain
   written Chinese, each naming the segment it starts at. Filler, repetition
   and false starts are removed and homophones and terms fixed. Paragraph
   breaks follow the meaning, and each paragraph takes the timestamp of its
   first segment. A paragraph whose rewrite looks like added or lost content
   falls back to the raw text of its segments with a warning; a chunk whose
   response is unusable falls back to rule-based paragraphs of raw text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from livedigest import llm, recording
from livedigest.config import PolishSettings
from livedigest.files import write_json
from livedigest.transcript import Segment, meta_lines, stamp

log = logging.getLogger(__name__)

# Phrases Whisper emits over silence or music, compared without punctuation.
HALLUCINATIONS = {
    "谢谢观看",
    "谢谢大家观看",
    "谢谢收看",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "字幕由Amara.org社区提供",
    "小编字幕由Amara.org社区提供",
    "优优独播剧场——YoYo Television Series Exclusive",
    "以下是普通话的直播内容请使用简体中文",
}
_PUNCT = re.compile(r"[\s，。！？、,.!?；;：:\"'“”‘’…—-]+")
_ENDS_SENTENCE = tuple("。！？!?…")
_CLAUSE_END = _ENDS_SENTENCE + tuple("吗呢吧啊嘛呀了哈啦")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BLOCKS_START = "<<<BLOCKS"
_BLOCKS_END = "BLOCKS>>>"

PROMPT = """\
{intro}的机器语音转写，按时间顺序切成很多短句，每句有 id。
转写可能有同音错字、术语和专有名词听错，标点也不可靠。读者只想读懂内容，不在乎口语原貌。

请把这些句子整理成干净、易读的书面中文段落：
- 大胆删掉口头禅、语气词和填充词（就是、那个、然后、其实、对、嗯、啊、呀、嘛、这样子等）、重复、口误和说到一半改口的部分。
- 修正明显的同音错字、术语和专有名词；术语表里有的，按术语表的写法。
- 保留全部实质内容：事实、数字、人名、产品、观点、理由、例子，以及"听说""不确定"这类表明把握程度的说法。
- 不要概括成摘要，不要补充原文没有的信息，不要调换内容的先后顺序。
- 按意思分段：一段讲一件事，通常两到六句话；不要在一句话中间断开。
- 主播念出观众弹幕或提问时，从那一句另起一段，写成"【问】问题本身？"，回答接在同一段里。
- 每段用 from 写出它从哪个 id 的句子开始。from 必须严格递增，第一段从第一个 id 开始，所有句子都要归入某一段；没有实质内容的句子并入相邻段落后删掉即可。
- 句子带 speaker 时，那是自动识别的说话人编号（S? 表示没认出是谁）：换人说话就另起一段，并只在换人后的第一段段首写"S02："这样的编号；同一个人接着说的段落不写编号，段落不能只有编号。
- 中英混杂时保留英文原词，英文和中文之间加空格。
- 拿不准的修正写进 unsure，格式"原文→改为"；没有就给空数组。

只输出一个 JSON 数组，不要任何其他文字或代码块标记：
[{{"from": 1, "text": "……", "unsure": []}}]

术语表：{glossary}
主播：{anchor}

{start}
{segments}
{end}
"""


# Opening of the prompt per meta["kind"]; {source} names the platform.
INTRO = {
    "live": "你是直播转写稿的编辑。下面是{source}",
    "video": "你是短视频转写稿的编辑。下面是{source}",
}


class PolishError(Exception):
    """No chunk could be polished."""


@dataclass
class Block:
    id: int
    start: float
    end: float
    raw: str
    text: str = ""
    unsure: list[str] = field(default_factory=list)
    warning: str | None = None


def _bare(text: str) -> str:
    return _PUNCT.sub("", text)


def clean_segments(segments: list[Segment]) -> list[Segment]:
    """Drop hallucinated stock phrases and immediate repeats of the same line."""
    kept: list[Segment] = []
    for seg in segments:
        bare = _bare(seg.text)
        if not bare or bare in HALLUCINATIONS:
            continue
        if kept and _bare(kept[-1].text) == bare:
            kept[-1].end = seg.end
            continue
        kept.append(Segment(seg.start, seg.end, seg.text.strip(), seg.speaker))
    return kept


def make_blocks(
    segments: list[Segment], max_chars: int, gap_seconds: float
) -> list[Block]:
    """Group segments into paragraphs, breaking at long pauses or length.

    Past ``max_chars`` a block ends only after a segment that closes a sentence
    or spoken clause, so paragraphs do not split mid-sentence; at 1.5 times the
    limit it ends regardless. Whisper's segments usually abut with no gap and
    no final punctuation, which is why trailing particles count as closers.
    """
    blocks: list[Block] = []
    parts: list[str] = []
    start = end = 0.0
    for seg in segments:
        size = sum(map(len, parts))
        closed = bool(parts) and parts[-1].rstrip().endswith(_CLAUSE_END)
        if parts and (
            seg.start - end > gap_seconds
            or (size >= max_chars and closed)
            or size >= max_chars * 1.5
        ):
            blocks.append(Block(len(blocks) + 1, start, end, _join(parts)))
            parts = []
        if not parts:
            start = seg.start
        parts.append(seg.text)
        end = seg.end
    if parts:
        blocks.append(Block(len(blocks) + 1, start, end, _join(parts)))
    return blocks


def _join(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if out and not out.endswith(_ENDS_SENTENCE + tuple("，,")):
            out += "，"
        out += part
    return out


def load_glossary(glossary_dir: Path, anchor: str) -> list[str]:
    """Terms from ``_common.txt`` and ``<anchor>.txt``; ``#`` starts a comment."""
    terms: list[str] = []
    for name in ("_common.txt", f"{anchor}.txt"):
        path = glossary_dir / name
        if not path.exists():
            continue
        for line in path.read_text("utf-8").splitlines():
            term = line.split("#", 1)[0].strip()
            if term and term not in terms:
                terms.append(term)
    return terms


def build_prompt(
    numbered: list[tuple[int, Segment]],
    anchor: str,
    glossary: list[str],
    kind: str = "live",
    platform: str | None = None,
) -> str:
    payload = json.dumps(
        [
            {"id": i, "text": seg.text}
            | ({"speaker": seg.speaker} if seg.speaker else {})
            for i, seg in numbered
        ],
        ensure_ascii=False,
        indent=0,
    )
    return PROMPT.format(
        intro=INTRO.get(kind, INTRO["live"]).format(
            source=recording.source_phrase(kind, platform)
        ),
        glossary="、".join(glossary) or "（无）",
        anchor=anchor,
        start=_BLOCKS_START,
        segments=payload,
        end=_BLOCKS_END,
    )


def parse_response(text: str) -> list[dict]:
    """Extract the JSON array from model output, tolerating stray prose."""
    first, last = text.find("["), text.rfind("]")
    if first < 0 or last < first:
        raise ValueError("no JSON array in response")
    data = json.loads(text[first : last + 1])
    if not isinstance(data, list):
        raise ValueError("response is not a list")
    return data


def apply_rewrite(block: Block, item: dict | None) -> None:
    """Accept a rewrite unless it looks like added or lost content."""
    if item is None:
        block.text, block.warning = block.raw, "missing from response"
        return
    text = str(item.get("text", "")).strip()
    block.unsure = [str(u) for u in item.get("unsure") or []]
    raw_len, new_len = len(_bare(block.raw)), len(_bare(text))
    if new_len > raw_len * 1.2 + 20:
        block.text, block.warning = block.raw, "rewrite longer than source"
    elif raw_len >= 60 and new_len < raw_len * 0.25:
        block.text, block.warning = block.raw, "rewrite removed too much"
    else:
        block.text = text
        missing = set(_NUMBER.findall(block.raw)) - set(_NUMBER.findall(text))
        if missing:
            block.warning = f"numbers changed: {', '.join(sorted(missing))}"


def _chunks(
    numbered: list[tuple[int, Segment]], chunk_chars: int
) -> list[list[tuple[int, Segment]]]:
    """Split into model calls, preferring to cut after a clause end."""
    chunks: list[list[tuple[int, Segment]]] = [[]]
    size = 0
    for item in numbered:
        current = chunks[-1]
        closed = bool(current) and current[-1][1].text.rstrip().endswith(_CLAUSE_END)
        if current and ((size >= chunk_chars and closed) or size >= chunk_chars * 1.2):
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += len(item[1].text)
    return [c for c in chunks if c]


def assemble(numbered: list[tuple[int, Segment]], items: list) -> list[Block]:
    """Map paragraphs onto the segment spans their ``from`` ids mark.

    Raises ValueError when the ids are unusable, so the chunk is retried.
    """
    index = {i: pos for pos, (i, _) in enumerate(numbered)}
    paragraphs = [item for item in items if isinstance(item, dict)]
    froms = [item.get("from") for item in paragraphs]
    if not froms or any(f not in index for f in froms):
        raise ValueError(f"paragraph ids outside this chunk: {froms}")
    positions = [index[f] for f in froms]
    if positions != sorted(set(positions)):
        raise ValueError(f"paragraph ids not strictly increasing: {froms}")
    # Leading segments the model folded away belong to the first paragraph.
    bounds = [0, *positions[1:], len(numbered)]
    blocks = []
    for n, item in enumerate(paragraphs):
        span = [seg for _, seg in numbered[bounds[n] : bounds[n + 1]]]
        first = numbered[positions[n]][1]
        block = Block(
            id=numbered[positions[n]][0],
            start=first.start,
            end=span[-1].end,
            raw=_join([seg.text for seg in span]),
        )
        apply_rewrite(block, item)
        blocks.append(block)
    return blocks


def polish(
    segments: list[Segment],
    anchor: str,
    glossary: list[str],
    settings: PolishSettings,
    kind: str = "live",
    platform: str | None = None,
) -> list[Block]:
    """Return polished paragraphs; failed chunks keep their raw text."""
    numbered = list(enumerate(clean_segments(segments), start=1))
    chunks = _chunks(numbered, settings.chunk_chars)
    blocks: list[Block] = []
    failed_chunks = 0
    last_error = ""
    for chunk in chunks:
        prompt = build_prompt(chunk, anchor, glossary, kind, platform)
        for _attempt in range(2):
            try:
                output = llm.run(settings.command, prompt, settings.timeout_seconds)
                blocks += assemble(chunk, parse_response(output))
            except (llm.LLMError, ValueError) as exc:
                last_error = str(exc)
                log.warning("polish chunk failed: %s", exc)
                continue
            break
        else:
            failed_chunks += 1
            fallback = make_blocks(
                [seg for _, seg in chunk],
                settings.max_block_chars,
                settings.block_gap_seconds,
            )
            for block in fallback:
                block.id = len(blocks) + 1
                block.text, block.warning = block.raw, f"chunk failed: {last_error}"
                blocks.append(block)
    if chunks and failed_chunks == len(chunks):
        raise PolishError(f"every chunk failed; last error: {last_error}")
    return blocks


def as_segments(blocks: list[Block]) -> list[Segment]:
    """Polished blocks as segments, for the digest prompt."""
    return [Segment(b.start, b.end, b.text) for b in blocks if b.text]


def save_blocks(blocks: list[Block], path: Path) -> None:
    write_json(path, [asdict(b) for b in blocks], indent=1)


def load_blocks(path: Path) -> list[Block]:
    return [Block(**item) for item in json.loads(path.read_text("utf-8"))]


def to_markdown(blocks: list[Block], title: str, meta: dict) -> str:
    lines = [f"# {title}", "", *meta_lines(meta)]
    lines += ["- 原始转写: [transcript.md](transcript.md)", ""]
    for block in blocks:
        if not block.text:
            continue
        # Blank lines from the model and each viewer question start a paragraph.
        paragraphs = []
        for chunk in re.split(r"\n\s*\n", block.text):
            head, *questions = chunk.split("【问】")
            paragraphs += [head.strip()] if head.strip() else []
            paragraphs += [f"**问：**{q.strip()}" for q in questions]
        lines += [f"**[{stamp(block.start)}]** {paragraphs[0]}", ""]
        lines += [p for para in paragraphs[1:] for p in (para, "")]
    notes = [
        f"- [{stamp(b.start)}] {note}"
        for b in blocks
        for note in b.unsure + ([f"⚠ {b.warning}"] if b.warning else [])
    ]
    if notes:
        lines += ["## 待核对", "", *notes, ""]
    return "\n".join(lines)
