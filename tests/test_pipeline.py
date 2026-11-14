import json
import os
import time

import pytest
from conftest import make_media

from livedigest import pipeline
from livedigest.engines import EngineError
from livedigest.transcript import Segment

SEGMENTS = [Segment(0.0, 1.0, "大家好"), Segment(1.0, 2.0, "就是今天讲直播")]


class FakeEngine:
    name = "fake"

    def __init__(self, error=None):
        self.calls = []
        self.terms = []
        self.error = error

    def transcribe(self, audio, hotwords=()):
        if self.error:
            raise EngineError(self.error)
        self.calls.append(audio)
        self.terms.append(list(hotwords))
        return list(SEGMENTS)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(pipeline, "get_engine", lambda settings: fake)
    return fake


@pytest.fixture
def fake_asr(engine):
    return engine.calls


def recording_path(settings, name="主播甲_2026-10-17_20-00-00.mp4"):
    return settings.recordings_dir / "小红书直播" / "主播甲" / name


def test_process_writes_all_outputs_and_is_idempotent(settings, fake_asr):
    media = make_media(recording_path(settings))
    result = pipeline.process(settings, media)
    job = result.job_dir
    assert job == settings.output_dir / "2026-10-17" / "主播甲" / media.stem
    assert {p.name for p in job.iterdir()} >= {
        "meta.json",
        "status.json",
        "transcript.json",
        "transcript.srt",
        "transcript.md",
        "polished.json",
        "transcript.clean.md",
        "digest.md",
    }
    meta = json.loads((job / "meta.json").read_text())
    assert meta["anchor"] == "主播甲"
    assert meta["platform"] == "小红书直播"
    assert meta["kind"] == "live"
    assert meta["started_at"] == "2026-10-17T20:00:00"
    assert meta["engine"] == "fake"
    clean = (job / "transcript.clean.md").read_text()
    assert "**[00:00:01]** 今天讲直播" in clean
    # The digest command is `cat`, so the prompt comes back: built from the
    # polished text, not the raw segments.
    digest = (job / "digest.md").read_text()
    assert "[00:00:01] 今天讲直播" in digest
    assert "就是" not in digest
    assert "[整理版](transcript.clean.md)" in digest
    index = json.loads((settings.output_dir / "index.json").read_text())
    assert index["jobs"][0]["digest"] == "done"

    again = pipeline.process(settings, media)
    assert again.skipped
    assert len(fake_asr) == 1


def test_downloaded_video_takes_metadata_from_info_file(settings, fake_asr):
    folder = settings.recordings_dir / "douyin-video" / "作者乙"
    media = make_media(folder / "标题 [7000000000000000001].mp4")
    media.with_suffix(".info.json").write_text(
        json.dumps(
            {
                "title": "标题",
                "published_at": "2026-07-21T02:39:04",
                "source_url": "https://www.douyin.com/video/7000000000000000001",
            }
        )
    )
    job = pipeline.process(settings, media).job_dir
    assert job == settings.output_dir / "2026-07-21" / "作者乙" / media.stem
    meta = json.loads((job / "meta.json").read_text())
    assert meta["anchor"] == "作者乙"
    assert meta["platform"] == "douyin-video"
    assert meta["source_url"] == "https://www.douyin.com/video/7000000000000000001"
    assert meta["title"] == "标题"
    assert meta["published_at"] == "2026-07-21T02:39:04"
    assert meta["kind"] == "video"
    digest = (job / "digest.md").read_text()
    assert "- 来源: https://www.douyin.com/video/" in digest
    # The digest command is `cat`: the prompt describes a video, not a live.
    assert "一条抖音短视频" in digest
    assert "直播信息" not in digest


@pytest.mark.parametrize("info", ['{"published_at": "yesterday"}', "not json", "[]"])
def test_video_with_bad_info_file_is_still_processed(settings, fake_asr, info):
    folder = settings.recordings_dir / "douyin-video" / "作者乙"
    media = make_media(folder / "标题 [7000000000000000001].mp4")
    media.with_suffix(".info.json").write_text(info)
    os.utime(media, (time.mktime((2026, 7, 1, 12, 0, 0, 0, 0, -1)),) * 2)
    result = pipeline.process(settings, media)
    assert result.job_dir.parent.parent.name == "2026-07-01"
    assert result.status["stages"]["digest"]["state"] == "done"


def test_digest_failure_is_recorded_and_retried_without_reasr(settings, fake_asr):
    media = make_media(recording_path(settings))
    settings.digest.command = ["false"]
    result = pipeline.process(settings, media)
    assert result.status["stages"]["transcribe"]["state"] == "done"
    assert result.status["stages"]["digest"]["state"] == "failed"
    failures = (settings.output_dir / "failures.jsonl").read_text().splitlines()
    assert json.loads(failures[0])["stage"] == "digest"

    assert pipeline.process(settings, media, retry_failed=False).skipped
    settings.digest.command = ["cat"]
    result = pipeline.process(settings, media)
    assert result.status["stages"]["digest"]["state"] == "done"
    assert len(fake_asr) == 1


def test_transcription_failure_is_separate_stage(settings, engine):
    engine.error = "model missing"
    media = make_media(recording_path(settings))
    result = pipeline.process(settings, media)
    assert result.status["stages"]["capture"]["state"] == "done"
    assert result.status["stages"]["transcribe"]["state"] == "failed"
    assert "digest" not in result.status["stages"]
    entry = json.loads((settings.output_dir / "failures.jsonl").read_text())
    assert entry["stage"] == "transcribe"


def test_changed_file_is_reprocessed(settings, fake_asr):
    media = make_media(recording_path(settings))
    pipeline.process(settings, media)
    make_media(media, seconds=3.0)
    assert not pipeline.process(settings, media).skipped
    assert len(fake_asr) == 2


def test_polish_failure_falls_back_to_raw_digest(settings, fake_asr):
    settings.polish.command = ["false"]
    media = make_media(recording_path(settings))
    result = pipeline.process(settings, media)
    assert result.status["stages"]["polish"]["state"] == "failed"
    assert result.status["stages"]["digest"]["state"] == "done"
    assert "就是今天讲直播" in (result.job_dir / "digest.md").read_text()


def test_redo_polish_reruns_digest_but_not_asr(settings, fake_asr):
    media = make_media(recording_path(settings))
    pipeline.process(settings, media)
    digest = pipeline.job_dir_for(settings, media, "主播甲") / "digest.md"
    digest.write_text("stale")
    pipeline.process(settings, media, redo="polish")
    assert digest.read_text() != "stale"
    assert len(fake_asr) == 1


def test_glossary_reaches_transcription(settings, engine):
    settings.glossary_dir.mkdir()
    (settings.glossary_dir / "_common.txt").write_text("券商\n", "utf-8")
    (settings.glossary_dir / "主播甲.txt").write_text(
        "# 注释\nSchwab  # 嘉信\n券商\n", "utf-8"
    )
    pipeline.process(settings, make_media(recording_path(settings)))
    assert engine.terms == [["券商", "Schwab"]]


def test_index_keeps_jobs_with_old_meta(settings, fake_asr):
    old = settings.output_dir / "2026-10-01" / "主播乙" / "old"
    old.mkdir(parents=True)
    (old / "meta.json").write_text('{"anchor": "主播乙"}', "utf-8")
    pipeline.process(settings, make_media(recording_path(settings)))
    jobs = json.loads((settings.output_dir / "index.json").read_text())["jobs"]
    assert {job["anchor"] for job in jobs} == {"主播甲", "主播乙"}
    assert "| ? | 主播乙 |" in (settings.output_dir / "index.md").read_text()
