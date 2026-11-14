import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from livedigest import capture
from livedigest.capture import (
    Capture,
    CaptureSettings,
    ClosedSegment,
    PartPaths,
    build_command,
    read_segment_list,
    tail_segments,
)
from livedigest.resolve import StreamInfo

FLV = StreamInfo(True, "bilibili", "a", "t", "https://cdn.example/x.flv?codec=avc")


def after(command, flag):
    return command[command.index(flag) + 1]


def outputs(command):
    """The output paths, i.e. arguments right after each output's last option."""
    return [arg for arg in command if "%0" in arg]


def test_audio_and_frames_command(tmp_path):
    paths = PartPaths(tmp_path, 3)
    command = build_command(FLV, paths, CaptureSettings(segment_seconds=60), "auto")
    assert "-re" not in command
    assert command.index("-skip_frame") < command.index("-i")
    assert after(command, "-i") == FLV.url
    assert after(command, "-segment_time") == "60"
    assert after(command, "-segment_format") == "mpegts"
    assert after(command, "-segment_format_options") == "flush_packets=1"
    assert after(command, "-b:a") == "48k" and after(command, "-ar") == "16000"
    assert after(command, "-map") == "0:a:0"
    assert "0:v:0?" in command
    assert after(command, "-vf") == r"fps=1/10,scale=-2:min(720\,ih)"
    assert after(command, "-user_agent") == capture.MOBILE_UA
    assert after(command, "-rw_timeout") == "15000000"
    assert after(command, "-referer") == "https://live.bilibili.com/"
    assert outputs(command) == [
        str(tmp_path / "audio" / "p03_%03d.ts"),
        str(tmp_path / "frames" / ".raw_p03_%06d.jpg"),
    ]
    assert after(command, "-segment_list") == str(paths.segment_list)


def test_audio_only_has_no_video_options(tmp_path):
    command = build_command(FLV, PartPaths(tmp_path, 1), CaptureSettings(), "audio")
    assert "-skip_frame" not in command and "-vf" not in command
    assert len(outputs(command)) == 1


def test_video_adds_stream_copy(tmp_path):
    command = build_command(FLV, PartPaths(tmp_path, 1), CaptureSettings(), "video")
    assert outputs(command)[-1] == str(tmp_path / "video" / "p01_%03d.ts")
    tail = command[command.index("-c") :]
    assert tail[:2] == ["-c", "copy"] and "1800" in tail


def test_youtube_headers_and_proxy(tmp_path):
    info = StreamInfo(
        True,
        "youtube",
        "a",
        "t",
        "https://x/index.m3u8",
        {"User-Agent": "UA/1", "Referer": "https://r/"},
    )
    settings = CaptureSettings(proxy="http://127.0.0.1:7890")
    command = build_command(info, PartPaths(tmp_path, 1), settings, "auto")
    assert after(command, "-rw_timeout") == "50000000"
    assert after(command, "-user_agent") == "UA/1"
    assert after(command, "-headers") == "Referer: https://r/\r\n"
    assert after(command, "-http_proxy") == "http://127.0.0.1:7890"
    assert "-referer" not in command


def test_offline_stream_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        build_command(
            StreamInfo(False, "douyin", "a", ""), PartPaths(tmp_path, 1), None, "auto"
        )


def test_read_segment_list_ignores_partial_row(tmp_path):
    paths = PartPaths(tmp_path, 2)
    paths.audio_dir.mkdir()
    paths.segment_list.write_text(
        "p02_000.ts,0.000000,60.032000\np02_001.ts,60.032000,120.0\np02_002.ts,120"
    )
    assert read_segment_list(paths) == [
        ClosedSegment(0, paths.audio_dir / "p02_000.ts", 0.0, 60.032),
        ClosedSegment(1, paths.audio_dir / "p02_001.ts", 60.032, 120.0),
    ]
    assert read_segment_list(PartPaths(tmp_path, 9)) == []


def write_ts(path, seconds):
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", f"sine=duration={seconds}:sample_rate=16000",
            "-c:a", "aac", "-b:a", "48k", "-f", "mpegts", str(path),
        ],
        check=True,
    )  # fmt: skip


def test_tail_segments_probes_unlisted_files(tmp_path):
    paths = PartPaths(tmp_path, 1)
    paths.audio_dir.mkdir()
    known = [ClosedSegment(0, paths.audio_dir / "p01_000.ts", 0.0, 4.0)]
    (paths.audio_dir / "p01_000.ts").write_bytes(b"listed")
    write_ts(paths.audio_dir / "p01_001.ts", 2)
    (paths.audio_dir / "p01_002.ts").write_bytes(b"")
    (paths.audio_dir / "p02_000.ts").write_bytes(b"other part")
    [tail] = tail_segments(paths, known)
    assert tail.index == 1 and tail.start == 4.0
    assert tail.end == pytest.approx(6.0, abs=0.1)
    assert not (paths.audio_dir / "p01_002.ts").exists()
    # Recorded, so offsets survive on disk and a second call finds nothing.
    [row] = read_segment_list(paths)
    assert (row.index, row.start) == (1, 4.0)
    assert row.end == pytest.approx(tail.end)
    assert tail_segments(paths, read_segment_list(paths)) == []


class FakeProcess:
    def __init__(self, exits_on=None, returncode=None):
        self.pid = 4242
        self.stdin = self
        self.written = b""
        self.calls = []
        self.exits_on = exits_on
        self.returncode = returncode

    def write(self, data):
        self.written += data

    def close(self):
        self.calls.append("close")

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.exits_on in self.calls:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("ffmpeg", timeout)

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


def start_fake(monkeypatch, tmp_path, process, keep="auto"):
    launched = []

    def popen(command, **kwargs):
        launched.append(command)
        assert kwargs["start_new_session"]
        return process

    fake_subprocess = SimpleNamespace(
        Popen=popen,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
        run=subprocess.run,
    )
    monkeypatch.setattr(capture, "subprocess", fake_subprocess)
    cap = Capture(FLV, PartPaths(tmp_path, 1), CaptureSettings(), keep)
    cap.start()
    return cap, launched


def test_stop_sends_q_then_escalates(monkeypatch, tmp_path):
    process = FakeProcess(exits_on="terminate")
    cap, _ = start_fake(monkeypatch, tmp_path, process)
    assert cap.running and cap.pid == 4242
    assert cap.stop(timeout=0) == 0
    assert process.written == b"q"
    assert process.calls == ["close", "terminate"]


def test_poll_returns_new_segments_then_tail(monkeypatch, tmp_path):
    process = FakeProcess()
    cap, _ = start_fake(monkeypatch, tmp_path, process)
    paths = cap.paths
    paths.segment_list.write_text("p01_000.ts,0,60\n")
    assert [s.index for s in cap.poll_closed_segments()] == [0]
    assert cap.poll_closed_segments() == []
    paths.segment_list.write_text("p01_000.ts,0,60\np01_001.ts,60,120\n")
    (paths.audio_dir / "p01_000.ts").write_bytes(b"x")
    (paths.audio_dir / "p01_001.ts").write_bytes(b"x")
    write_ts(paths.audio_dir / "p01_002.ts", 1)
    process.returncode = 255
    segments = cap.poll_closed_segments()
    assert [s.index for s in segments] == [1, 2]
    assert segments[1].start == 120
    assert cap.finished and cap.poll_closed_segments() == []


def test_no_video_track_restarts_audio_only(monkeypatch, tmp_path):
    process = FakeProcess(returncode=234)
    cap, launched = start_fake(monkeypatch, tmp_path, process)
    cap.paths.log_path.write_text("Output file does not contain any stream\n")
    assert cap.poll_closed_segments() == []
    assert len(launched) == 2
    assert "-vf" in launched[0] and "-vf" not in launched[1]
    assert not cap.finished


def test_local_input_produces_listed_segments_and_stills(tmp_path):
    """The real argv against a local file (no network): outputs are valid."""
    source = tmp_path / "src.flv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=size=320x240:rate=25:duration=35",
            "-f", "lavfi", "-i", "sine=duration=35", "-c:v", "libx264",
            "-g", "50", "-c:a", "aac", "-shortest", str(source),
        ],
        check=True,
    )  # fmt: skip
    info = StreamInfo(True, "bilibili", "a", "t", str(source))
    paths = PartPaths(tmp_path / "s", 1)
    settings = CaptureSettings(segment_seconds=10)
    for directory in (paths.audio_dir, paths.frames_dir, paths.video_dir):
        directory.mkdir(parents=True)
    subprocess.run(
        build_command(info, paths, settings, "video"),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    segments = read_segment_list(paths)
    assert [s.index for s in segments] == [0, 1, 2, 3]
    assert segments[0].end == pytest.approx(10, abs=0.1)
    assert [p.name for p in paths.raw_frames()] == [
        ".raw_p01_000000.jpg",
        ".raw_p01_000001.jpg",
        ".raw_p01_000002.jpg",
    ]
    assert len(list(paths.video_dir.glob("p01_*.ts"))) == 1


def test_stop_hands_on_the_short_final_segment(tmp_path):
    """``q`` closes the open segment; it comes back from the final poll."""
    fifo = tmp_path / "live.ts"
    os.mkfifo(fifo)
    producer = subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-readrate", "5", "-f", "lavfi",
            "-i", "sine=duration=120", "-c:a", "aac", "-f", "mpegts", "-y", str(fifo),
        ],
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        info = StreamInfo(True, "bilibili", "a", "t", str(fifo))
        cap = Capture(info, PartPaths(tmp_path / "s", 1), CaptureSettings(2), "audio")
        cap.start()
        seen = []
        deadline = time.monotonic() + 20
        while len(seen) < 2 and time.monotonic() < deadline:
            seen += cap.poll_closed_segments()
            time.sleep(0.2)
        time.sleep(0.5)
        cap.stop()
        seen += cap.poll_closed_segments()
    finally:
        producer.kill()
        producer.wait()
    assert cap.finished
    on_disk = sorted(p.name for p in cap.paths.audio_dir.glob("p01_*.ts"))
    assert [s.path.name for s in seen] == on_disk
    assert seen[-1].end - seen[-1].start < 2
