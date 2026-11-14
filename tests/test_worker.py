import json
import time

import pytest

from livedigest import cli, pipeline, worker
from livedigest.capture import PartPaths
from livedigest.engines import EngineError
from livedigest.session import Session, segment_refs, transcript_path
from livedigest.transcript import Segment

T0 = 1_800_000_000.0
ROOM = "https://live.bilibili.com/1000001"


class FakeEngine:
    name = "fake-asr"

    def __init__(self, fail_on=(), crash_on=()):
        self.calls = []
        self.fail_on = set(fail_on)
        self.crash_on = set(crash_on)

    def transcribe(self, audio, hotwords=()):
        if audio.name in self.fail_on:
            raise EngineError(f"cannot read {audio.name}")
        if audio.name in self.crash_on:
            raise RuntimeError("Metal out of memory")
        self.calls.append(audio.name)
        return [Segment(1.0, 2.0, f"第{len(self.calls)}句")]


def add_segments(session, count, part_segments=None):
    """Write ``count`` 60 s segments into the session's last part."""
    part = session.current_part
    paths = PartPaths(session.dir, part.index)
    rows = ""
    for i in range(count):
        (paths.audio_dir / f"p{part.index:02d}_{i:03d}.ts").write_bytes(b"x")
        rows += f"p{part.index:02d}_{i:03d}.ts,{i * 60},{(i + 1) * 60}\n"
    paths.segment_list.write_text(rows)
    part.segments = count if part_segments is None else part_segments
    session.save()


def make_session(settings, count=2, ended=True):
    session = Session.create(
        settings.recordings_dir, "bilibili", "主播甲", "讲座", ROOM, "auto", T0
    )
    session.start_part(T0 + 10)
    add_segments(session, count)
    if ended:
        session.end()
        session.save()
    return session


def job_of(settings, session):
    return pipeline.session_job_dir(settings, Session.load(session.dir))


def test_scan_transcribes_shifts_times_and_finalizes(settings):
    session = make_session(settings)
    engine = FakeEngine()
    w = worker.Worker(settings, engine)
    assert w.scan() == 2
    w.drain()
    first = json.loads(transcript_path(segment_refs(session)[0]).read_text())
    assert (first["start"], first["engine"]) == (10.0, "fake-asr")
    job = job_of(settings, session)
    lines = json.loads((job / "transcript.json").read_text())
    assert [(s["start"], s["text"]) for s in lines] == [
        (11.0, "第1句"),
        (71.0, "第2句"),
    ]
    meta = json.loads((job / "meta.json").read_text())
    assert meta["platform"] == "bilibili" and meta["source_url"] == ROOM
    assert meta["title"] == "讲座" and meta["parts"] == 1
    assert meta["engine"] == "fake-asr" and meta["state"] == "ended"
    status = json.loads((job / "status.json").read_text())
    assert status["finalized_at"]
    assert {k: v["state"] for k, v in status["stages"].items()} == {
        "transcribe": "done",
        "polish": "done",
        "digest": "done",
    }
    # The digest command is `cat`: the prompt names the platform.
    assert "一场B站直播" in (job / "digest.md").read_text()

    again = worker.Worker(settings, engine)
    assert again.scan() == 0
    again.drain()
    assert engine.calls == ["p01_000.ts", "p01_001.ts"]


def test_transcript_grows_while_live(settings):
    session = make_session(settings, count=1, ended=False)
    w = worker.Worker(settings, FakeEngine())
    w.scan()
    w.drain()
    job = job_of(settings, session)
    assert "[00:00:11] 第1句" in (job / "transcript.md").read_text()
    status = json.loads((job / "status.json").read_text())
    assert status["stages"]["transcribe"]["state"] == "live"
    assert not (job / "digest.md").exists()

    add_segments(session, 2)
    w.on_segment(segment_refs(Session.load(session.dir))[1])
    w.drain()
    text = (job / "transcript.md").read_text()
    assert "[00:00:11] 第1句" in text and "[00:01:11] 第2句" in text
    index = json.loads((settings.output_dir / "index.json").read_text())
    assert index["jobs"][0]["transcribe"] == "live"


def test_failed_segment_is_recorded_then_retried_on_next_start(settings):
    session = make_session(settings)
    w = worker.Worker(settings, FakeEngine(fail_on={"p01_001.ts"}))
    w.scan()
    w.drain()
    assert w.failed == 1
    failure = json.loads((settings.output_dir / "failures.jsonl").read_text())
    assert failure["stage"] == "transcribe"
    job = job_of(settings, session)
    status = json.loads((job / "status.json").read_text())
    assert status["stages"]["transcribe"]["state"] == "partial"
    (job / "digest.md").write_text("stale")

    engine = FakeEngine()
    retry = worker.Worker(settings, engine)
    assert retry.scan() == 1
    retry.drain()
    assert engine.calls == ["p01_001.ts"]
    status = json.loads((job / "status.json").read_text())
    assert status["stages"]["transcribe"]["state"] == "done"
    # The transcript changed, so the digest was written again.
    assert (job / "digest.md").read_text() != "stale"


def test_unexpected_engine_errors_are_recorded_too(settings):
    session = make_session(settings)
    engine = FakeEngine(crash_on={"p01_000.ts"})
    w = worker.Worker(settings, engine)
    w.start()
    try:
        for ref in segment_refs(session):
            w.on_segment(ref)
        deadline = time.monotonic() + 10
        while engine.calls != ["p01_001.ts"] and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        w.stop(timeout=5)
    assert engine.calls == ["p01_001.ts"]  # the thread survived the crash
    assert w.failed == 1
    failure = json.loads((settings.output_dir / "failures.jsonl").read_text())
    assert failure["stage"] == "transcribe"
    assert failure["error"] == "RuntimeError: Metal out of memory"
    retry = worker.Worker(settings, FakeEngine())
    assert retry.scan() == 1


def test_slide_stills_are_embedded_in_both_transcripts(settings):
    session = make_session(settings)
    session.visual = {"mode": "slides"}
    session.save()
    (session.frames_dir / "p01_000070.jpg").write_bytes(b"jpg")
    w = worker.Worker(settings, FakeEngine())
    w.scan()
    w.drain()
    job = job_of(settings, session)
    assert (job / "frames" / "p01_000070.jpg").exists()
    for name in ("transcript.md", "transcript.clean.md"):
        text = (job / name).read_text()
        image = text.index("![00:01:10](frames/p01_000070.jpg)")
        assert text.index("第1句") < image < text.index("第2句")


def test_camera_stills_are_not_embedded(settings):
    session = make_session(settings)
    session.visual = {"mode": "camera"}
    session.save()
    (session.frames_dir / "p01_000070.jpg").write_bytes(b"jpg")
    w = worker.Worker(settings, FakeEngine())
    w.scan()
    w.drain()
    job = job_of(settings, session)
    assert "frames/" not in (job / "transcript.md").read_text()
    assert not (job / "frames").exists()


def test_threads_transcribe_then_finish_and_stop(settings):
    session = make_session(settings, count=1, ended=False)
    w = worker.Worker(settings, FakeEngine())
    w.start()
    try:
        w.on_segment(segment_refs(session)[0])
        session.end()
        session.save()
        w.on_session_end(session.dir)
        status_path = job_of(settings, session) / "status.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if status_path.exists() and "finalized_at" in status_path.read_text():
                break
            time.sleep(0.05)
        else:
            pytest.fail("broadcast was not finalized")
    finally:
        w.stop(timeout=5)
    assert not any(t.is_alive() for t in w._threads)


def test_finish_ends_crashed_sessions_and_hands_on_their_tail(settings, monkeypatch):
    session = make_session(settings, ended=False)
    add_segments(session, 2, part_segments=1)  # crash before handing on the 2nd
    engine = FakeEngine()
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(worker, "get_engine", lambda s: engine)
    assert cli.main(["finish"]) == 0
    assert engine.calls == ["p01_000.ts", "p01_001.ts"]
    loaded = Session.load(session.dir)
    assert loaded.state == "ended" and loaded.parts[0].segments == 2
    status = json.loads((job_of(settings, session) / "status.json").read_text())
    assert status["stages"]["digest"]["state"] == "done"


def test_finish_refuses_while_watch_runs(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    with cli._watch_lock(settings) as held:
        assert held
        assert cli.main(["finish"]) == 1
    assert "watch" in capsys.readouterr().out
