import json
from pathlib import Path

import pytest

from livedigest import cli, compare


def write_run(folder: Path, engine: str, updated_at: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    stages = {
        "transcribe": {
            "state": "done",
            "engine": engine,
            "segments": 1,
            "seconds": 2.0,
        },
        "polish": {"state": "done", "blocks": 1, "raw_chars": 3, "clean_chars": 3},
    }
    (folder / "status.json").write_text(
        json.dumps({"stages": stages, "updated_at": updated_at}), "utf-8"
    )
    (folder / "transcript.json").write_text(
        json.dumps([{"start": 1.0, "end": 2.0, "text": text, "speaker": None}]),
        "utf-8",
    )
    (folder / "transcript.clean.md").write_text(f"**[00:00:01]** {text}\n", "utf-8")
    (folder / "digest.md").write_text(f"# t\n\n## 要点\n- [00:00:01] {text}\n", "utf-8")


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "主播甲_2026-10-17_20-00-00"
    write_run(run, "qwen3-asr/Qwen3-ASR-1.7B-8bit", "2026-10-22T09:00:00", "新的<b>")
    write_run(run / "mlx-whisper", "mlx-whisper", "2026-10-17T20:00:00", "旧的")
    (run / "meta.json").write_text(
        json.dumps(
            {"anchor": "主播甲", "duration_seconds": 40, "duration": "00:00:40"}
        ),
        "utf-8",
    )
    return run


def test_runs_are_kept_folders_then_current(run_dir):
    runs = compare.find_runs(run_dir)
    assert [r.folder for r in runs] == [run_dir / "mlx-whisper", run_dir]
    assert [r.label for r in runs] == ["mlx-whisper", "qwen3-asr · Qwen3-ASR-1.7B-8bit"]
    legacy = compare.EngineRun(run_dir, "baocut/qwen3-asr-1.7b@mlx-8bit", "", {})
    assert legacy.label == "BaoCut · qwen3-asr-1.7b"


def test_page_has_a_column_per_engine_and_escapes_text(run_dir):
    (run_dir / compare.NOTES_FILE).write_text("# 结论\n- **快** 一样\n", "utf-8")
    page = compare.build(run_dir).read_text("utf-8")
    assert "--cols: 2;" in page
    assert page.count('class="cell e2"') == page.count('class="cell e1"') > 0
    assert "新的&lt;b&gt;" in page and "旧的" in page
    assert "<h2>结论</h2>" in page and "<strong>快</strong>" in page
    assert "{{" not in page


def test_cli_needs_two_runs(run_dir, tmp_path, capsys):
    lone = tmp_path / "lone"
    write_run(lone, "mlx-whisper", "2026-10-17T20:00:00", "只有一个")
    assert cli.main(["compare", str(lone)]) == 1
    assert "1 engine run" in capsys.readouterr().err
    assert cli.main(["compare", str(run_dir)]) == 0
    assert (run_dir / "compare.html").exists()
