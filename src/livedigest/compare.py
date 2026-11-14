"""Side-by-side HTML page comparing ASR engines on one recording.

A run directory holds the current outputs. Earlier outputs kept in a
subdirectory (for example ``mlx-whisper/`` copied there before
``process --redo transcribe``) count as further engines. Each column shows the
engine's digest, polished transcript and raw ASR.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path

# Seconds of recording per aligned row.
ROW_SECONDS = 30
# Engine colour slots defined in compare.html.
MAX_ENGINES = 4
NOTES_FILE = "compare-notes.md"
PAGE_FILE = "compare.html"


@dataclass
class EngineRun:
    folder: Path
    engine: str
    updated_at: str
    stages: dict

    @property
    def label(self) -> str:
        # e.g. "qwen3-asr/Qwen3-ASR-1.7B-8bit"; older runs say "baocut/<model>"
        # or plain "mlx-whisper".
        name, _, model = self.engine.partition("/")
        if name == "baocut":
            return f"BaoCut · {model.split('@')[0]}" if model else "BaoCut"
        return f"{name} · {model}" if model else name

    def note(self, run_dir: Path) -> str:
        where = "current" if self.folder == run_dir else f"{self.folder.name}/"
        try:
            day = datetime.fromisoformat(self.updated_at).strftime("%-d %b")
        except ValueError:
            day = ""
        return " · ".join(x for x in (where, day) if x)


def find_runs(run_dir: Path) -> list[EngineRun]:
    """Kept earlier runs, oldest first, followed by the current one."""
    runs = []
    for folder in [*sorted(p for p in run_dir.iterdir() if p.is_dir()), run_dir]:
        status_path = folder / "status.json"
        if not (status_path.exists() and (folder / "transcript.json").exists()):
            continue
        status = json.loads(status_path.read_text("utf-8"))
        stages = status.get("stages", {})
        runs.append(
            EngineRun(
                folder=folder,
                engine=stages.get("transcribe", {}).get("engine") or "unknown",
                updated_at=status.get("updated_at", ""),
                stages=stages,
            )
        )
    kept = sorted((r for r in runs if r.folder != run_dir), key=lambda r: r.updated_at)
    return kept + [r for r in runs if r.folder == run_dir]


def build(run_dir: Path) -> Path:
    """Write ``compare.html`` into ``run_dir`` and return its path."""
    runs = find_runs(run_dir)
    if len(runs) < 2:
        raise ValueError(
            f"{run_dir} has {len(runs)} engine run(s); keep an earlier run in a "
            "subdirectory to compare against"
        )
    if len(runs) > MAX_ENGINES:
        raise ValueError(f"at most {MAX_ENGINES} engine runs fit on one page")
    meta = json.loads((run_dir / "meta.json").read_text("utf-8"))
    duration = float(meta.get("duration_seconds") or 0)
    keys = [f"e{i + 1}" for i in range(len(runs))]
    columns = list(zip(keys, runs, strict=True))

    def cells(items_for) -> str:
        return "".join(
            f'<div class="cell {key}" data-engine="{_esc(run.label)}">'
            f"{items_for(run) or '<p class=empty>—</p>'}</div>"
            for key, run in columns
        )

    digests = {run.folder: _digest_sections(run.folder / "digest.md") for run in runs}
    section_names = dict.fromkeys(n for d in digests.values() for n in d)
    digest_rows = "".join(
        f'<div class="row"><div class="bin">{_esc(name)}</div>'
        + cells(lambda run, name=name: _render_block(digests[run.folder].get(name, [])))
        + "</div>"
        for name in section_names
    )

    clean = {
        run.folder: [
            (start, f"<p>{_inline(text)}</p>")
            for start, text in _clean_blocks(run.folder / "transcript.clean.md")
        ]
        for run in runs
    }
    raw = {
        run.folder: [
            (
                seg["start"],
                f'<p><span class="ts">{_hms(seg["start"])}</span>{_esc(seg["text"])}'
                f'<span class="dur">{seg["end"] - seg["start"]:.1f}s</span></p>',
            )
            for seg in json.loads((run.folder / "transcript.json").read_text("utf-8"))
        ]
        for run in runs
    }

    def timed_rows(items: dict[Path, list[tuple[float, str]]]) -> str:
        rows = []
        for start in range(0, int(duration) + 1, ROW_SECONDS):
            body = cells(
                lambda run, start=start: "".join(
                    h for t, h in items[run.folder] if start <= t < start + ROW_SECONDS
                )
            )
            rows.append(
                f'<div class="row"><div class="bin">{_hms(start)}</div>{body}</div>'
            )
        return "".join(rows)

    head = "".join(
        f'<span class="{key}">{_esc(run.label)}</span>' for key, run in columns
    )
    legend = "".join(
        f'<span><span class="sw {key}"></span>{_esc(run.label)} · '
        f"{_esc(run.note(run_dir))}</span>"
        for key, run in columns
    )
    stat_head = "".join(
        f'<th scope=col class="{key}">{_esc(run.label)}</th>' for key, run in columns
    )
    stat_rows = "".join(
        f"<tr><th scope=row>{name}</th>"
        + "".join(
            f'<td class="{key}">{_stat(run.stages, stage, field, unit)}</td>'
            for key, run in columns
        )
        + "</tr>"
        for name, stage, field, unit in STATS
    )

    notes_path = run_dir / NOTES_FILE
    notes = _render_notes(notes_path.read_text("utf-8")) if notes_path.exists() else ""

    page = (
        files("livedigest")
        .joinpath("compare.html")
        .read_text("utf-8")
        .replace("{{COLS}}", str(len(runs)))
        .replace("{{ANCHOR}}", _esc(meta.get("anchor", "")))
        .replace("{{STARTED}}", _esc(meta.get("started_at", "").replace("T", " ")))
        .replace("{{DURATION}}", _esc(meta.get("duration", "")))
        .replace("{{NOTES}}", notes)
        .replace("{{INTRO_CLASS}}", "intro" if notes else "intro solo")
        .replace("{{STATHEAD}}", stat_head)
        .replace("{{STATS}}", stat_rows)
        .replace("{{LEGEND}}", legend)
        .replace("{{COLHEAD}}", f'<div class="colhead"><span></span>{head}</div>')
        .replace("{{DIGEST}}", digest_rows)
        .replace("{{CLEAN}}", timed_rows(clean))
        .replace("{{RAW}}", timed_rows(raw))
    )
    out = run_dir / PAGE_FILE
    out.write_text(page, "utf-8")
    return out


# (row label, status stage, field, unit)
STATS = [
    ("Segments from ASR", "transcribe", "segments", ""),
    ("Transcription time", "transcribe", "seconds", " s"),
    ("Raw characters", "polish", "raw_chars", ""),
    ("Polished paragraphs", "polish", "blocks", ""),
    ("Polished characters", "polish", "clean_chars", ""),
    ("Polish time", "polish", "seconds", " s"),
]


def _stat(stages: dict, stage: str, field: str, unit: str) -> str:
    value = stages.get(stage, {}).get(field)
    if value is None:
        return "—"
    return f"{value:.1f}{unit}" if isinstance(value, float) else f"{value}{unit}"


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(text: str) -> str:
    """Escape Markdown inline text, keeping bold, code and timestamps."""
    text = _esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    return re.sub(r"\[(\d\d:\d\d:\d\d)\]", r'<span class="ts">\1</span>', text)


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _seconds(stamp: str) -> int:
    h, m, s = (int(x) for x in stamp.split(":"))
    return h * 3600 + m * 60 + s


def _digest_sections(path: Path) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    if not path.exists():
        return sections
    current = None
    for line in path.read_text("utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.strip():
            sections[current].append(line.strip())
    return sections


def _render_block(lines: list[str]) -> str:
    items = [line[2:] for line in lines if line.startswith("- ")]
    if items and len(items) == len(lines):
        return "<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>"
    return "".join(f"<p>{_inline(line)}</p>" for line in lines)


def _clean_blocks(path: Path) -> list[tuple[int, str]]:
    if not path.exists():
        return []
    blocks = []
    for para in path.read_text("utf-8").split("\n\n"):
        m = re.match(r"\*\*\[(\d\d:\d\d:\d\d)\]\*\*\s*(.*)", para.strip(), re.S)
        if m:
            blocks.append((_seconds(m.group(1)), m.group(2).strip()))
    return blocks


def _render_notes(text: str) -> str:
    """Render the hand-written notes file: an optional heading and a list."""
    title = "Notes"
    items = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line[2:]
        elif m := re.match(r"(?:[-*]|\d+\.)\s+(.*)", line):
            items.append(m.group(1))
    if not items:
        return ""
    body = "".join(f"<li>{_inline(item)}</li>" for item in items)
    return f'<div class="notes"><h2>{_esc(title)}</h2><ol>{body}</ol></div>'
