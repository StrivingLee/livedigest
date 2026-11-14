"""Turn a timed transcript into a short digest with a command-line LLM."""

from __future__ import annotations

import subprocess

from livedigest import recording
from livedigest.config import DigestSettings
from livedigest.transcript import Segment, compact_lines

PROMPT = """\
你会收到{source}的自动语音转写稿，每行以 [HH:MM:SS] 开头，是距{start}的时间。
转写由机器完成，可能经过整理，仍可能有同音错字、人名和术语听错。

请用{language}写一份摘要，直接输出 Markdown，不要寒暄或解释：

## 概要
三到五句话，说明{this}讲了什么。

## 要点
按出现顺序列出要点，每条以对应的 [HH:MM:SS] 开头，便于回到{media}核对。

## 值得核对
列出转写可能出错、且会影响理解的人名、数字、产品名或说法，附时间戳。没有就写"无"。

规则：只依据转写稿，不补充稿外信息；拿不准的地方明确标注。

{info}：
{meta}

转写稿：
{transcript}
"""


# Wording per meta["kind"]; "source" comes from recording.source_phrase.
WORDS = {
    "live": {
        "start": "录制开始",
        "this": "这场直播",
        "media": "录像",
        "info": "直播信息",
    },
    "video": {
        "start": "视频开头",
        "this": "这条视频",
        "media": "视频",
        "info": "视频信息",
    },
}


class DigestError(Exception):
    """The digest command failed or produced nothing."""


def build_prompt(segments: list[Segment], meta: dict, language: str) -> str:
    meta_lines = "\n".join(
        f"- {key}: {meta[key]}"
        for key in ("anchor", "source_url", "started_at", "duration")
        if meta.get(key)
    )
    return PROMPT.format(
        **WORDS.get(meta.get("kind"), WORDS["live"]),
        source=recording.source_phrase(meta.get("kind"), meta.get("platform")),
        language=language,
        meta=meta_lines or "- (无)",
        transcript=compact_lines(segments),
    )


def write_digest(segments: list[Segment], meta: dict, settings: DigestSettings) -> str:
    """Pipe the prompt to ``settings.command`` on stdin and return its output."""
    prompt = build_prompt(segments, meta, settings.language)
    try:
        result = subprocess.run(
            settings.command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=settings.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DigestError(f"digest command not found: {settings.command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DigestError(f"digest timed out after {exc.timeout}s") from exc
    if result.returncode != 0:
        raise DigestError(
            f"digest command exited {result.returncode}: {result.stderr[-2000:]}"
        )
    text = result.stdout.strip()
    if not text:
        raise DigestError("digest command returned no text")
    return text
