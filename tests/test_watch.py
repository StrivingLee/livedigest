import pytest

from livedigest import visual, watch
from livedigest.capture import CaptureSettings, ClosedSegment, PartPaths
from livedigest.resolve import ResolveError, StreamInfo
from livedigest.rooms import Room, load_rooms, save_rooms
from livedigest.session import STATE_ENDED, STATE_LIVE, Session, epoch

T0 = 1_800_000_000.0
LIVE = StreamInfo(True, "bilibili", "主播", "讲座", "https://cdn/x.flv")
OFFLINE = StreamInfo(False, "bilibili", "主播", "")


class FakeCapture:
    instances: list["FakeCapture"] = []

    def __init__(self, info, paths, settings, keep):
        self.info, self.paths, self.keep = info, paths, keep
        self.pending: list[ClosedSegment] = []
        self.exited = self.finished = self.running = self.stopped = False
        self.pid = 4000 + len(FakeCapture.instances)
        self.returncode = None
        self.exit_code = 0
        self.error = ""
        FakeCapture.instances.append(self)

    def start(self):
        self.running = True

    def poll_closed_segments(self):
        out, self.pending = self.pending, []
        if self.exited:
            self.finished, self.running = True, False
            self.returncode = self.exit_code
        return out

    def last_error(self):
        return self.error

    def stop(self):
        self.stopped = self.exited = True

    def add_segment(self, index, start, end, frames=0):
        path = self.paths.audio_dir / f"p{self.paths.part:02d}_{index:03d}.ts"
        path.write_bytes(b"ts")
        first = int(start // 10)
        for number in range(first, first + frames):
            name = f".raw_p{self.paths.part:02d}_{number:06d}.jpg"
            (self.paths.frames_dir / name).write_bytes(b"")
        self.pending.append(ClosedSegment(index, path, start, end))


class Harness:
    def __init__(self, tmp_path, rooms, free=100.0):
        self.now = T0
        self.answers = {}
        self.free = free
        self.segments = []
        self.ended = []
        self.rooms_path = tmp_path / "rooms.toml"
        save_rooms(self.rooms_path, rooms)
        self.settings = CaptureSettings(poll_seconds=60, merge_gap_seconds=600)
        self.watcher = watch.Watcher(
            tmp_path / "root",
            self.rooms_path,
            self.settings,
            on_segment=self.segments.append,
            on_session_end=self.ended.append,
            resolver=self.resolve,
            capture_factory=FakeCapture,
            clock=lambda: self.now,
            free_gb=lambda path: self.free,
        )
        self.watcher.root.mkdir()
        self.resolved = []

    def resolve(self, room, settings):
        self.resolved.append(room.url)
        answer = self.answers.get(room.url, OFFLINE)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def tick(self, at=None):
        if at is not None:
            self.now = at
        self.watcher.tick()

    def session(self):
        [path] = list(self.watcher.root.glob("*/*/*/session.json"))
        return Session.load(path.parent)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeCapture.instances = []
    monkeypatch.setattr(visual, "dhash", lambda path: 0)


BILI = "https://live.bilibili.com/1"


def test_session_lifecycle_with_reconnect(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = LIVE
    h.tick()
    [first] = FakeCapture.instances
    session = h.session()
    assert session.anchor == "主播" and session.parts[0].pid == first.pid

    first.add_segment(0, 0.0, 60.0, frames=6)
    h.tick(T0 + 62)
    [ref] = h.segments
    assert (ref.part, ref.index, ref.start, ref.end) == (1, 0, 0.0, 60.0)
    assert [p.name for p in session.frames_dir.iterdir()] == ["p01_000000.jpg"]

    first.exited = True
    first.add_segment(1, 60.0, 75.0, frames=2)
    h.tick(T0 + 80)
    assert [s.index for s in h.segments] == [0, 1]
    assert h.session().parts[0].ended_at is not None

    h.tick(T0 + 82)  # within the retry delay: no lookup yet
    assert len(FakeCapture.instances) == 1
    h.tick(T0 + 100)
    second = FakeCapture.instances[1]
    session = h.session()
    assert [p.index for p in session.parts] == [1, 2]
    assert session.parts[1].offset_seconds == 100.0

    second.add_segment(0, 0.0, 30.0)
    second.exited = True
    h.answers[BILI] = OFFLINE
    h.tick(T0 + 130)
    assert (h.segments[-1].part, h.segments[-1].start) == (2, 100.0)
    assert h.segments[-1].end == 130.0

    for at in range(int(T0) + 150, int(T0) + 720, 30):
        h.tick(at)
    assert h.ended == []
    h.tick(T0 + 731)
    assert h.ended == [session.dir]
    session = h.session()
    assert session.state == STATE_ENDED
    assert epoch(session.ended_at) == T0 + 130
    assert session.visual["kept_frames"] == 1


def test_new_broadcast_after_gap_is_a_new_session(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = LIVE
    h.tick()
    FakeCapture.instances[0].exited = True
    h.answers[BILI] = OFFLINE
    h.tick(T0 + 10)
    for at in range(int(T0) + 30, int(T0) + 700, 30):
        h.tick(at)
    assert len(h.ended) == 1
    h.answers[BILI] = LIVE
    h.tick(T0 + 2000)
    assert len(list(h.watcher.root.glob("*/*/*/session.json"))) == 2


def test_callback_errors_do_not_stop_the_loop(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])

    def boom(ref):
        raise RuntimeError("downstream broke")

    h.watcher.on_segment = boom
    h.answers[BILI] = LIVE
    h.tick()
    FakeCapture.instances[0].add_segment(0, 0, 60)
    h.tick(T0 + 61)
    assert h.session().parts[0].segments == 1


def test_resolve_errors_are_retried(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = ResolveError("network down")
    h.tick()
    h.tick(T0 + 30)
    assert h.resolved == [BILI]
    h.answers[BILI] = LIVE
    h.tick(T0 + 61)
    assert len(FakeCapture.instances) == 1


def test_disabled_rooms_are_ignored(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili", enabled=False)])
    h.tick()
    assert h.resolved == []


def test_single_use_xhs_room_is_disabled_after_its_session(tmp_path):
    url = "https://xhslink.com/m/abc"
    h = Harness(tmp_path, [Room(url, "xiaohongshu")])
    h.answers[url] = StreamInfo(True, "xiaohongshu", "博主", "", "http://x.flv")
    h.tick()
    FakeCapture.instances[0].exited = True
    h.answers[url] = OFFLINE
    for at in range(int(T0) + 10, int(T0) + 700, 30):
        h.tick(at)
    assert len(h.ended) == 1
    assert load_rooms(h.rooms_path)[0].enabled is False


def test_disk_guard(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")], free=3.0)
    h.answers[BILI] = LIVE
    h.tick()
    assert h.resolved == [] and FakeCapture.instances == []
    h.free = 50.0
    h.tick(T0 + 61)
    capture = FakeCapture.instances[0]
    h.free = 0.5
    h.tick(T0 + 70)
    assert capture.stopped
    h.tick(T0 + 100)
    assert len(FakeCapture.instances) == 1  # no restart while the disk is full


def test_shutdown_leaves_session_resumable(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = LIVE
    h.tick()
    capture = FakeCapture.instances[0]
    capture.add_segment(0, 0, 20)
    h.watcher.shutdown()
    assert capture.stopped
    session = h.session()
    assert session.state == STATE_LIVE and session.parts[0].ended_at is not None
    assert len(h.segments) == 1


def crashed_session(h, segments_reported=0):
    """A session left live by a crash: part 1 open, two segments listed."""
    session = Session.create(h.watcher.root, "bilibili", "主播", "", BILI, "auto", T0)
    session.start_part(T0)
    session.parts[0].segments = segments_reported
    session.touch(T0 + 120)
    session.save()
    paths = PartPaths(session.dir, 1)
    paths.segment_list.write_text("p01_000.ts,0,60\np01_001.ts,60,120\n")
    return session


def test_recovery_continues_a_live_room(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    session = crashed_session(h, segments_reported=1)
    h.now = T0 + 200
    h.watcher.recover()
    assert [(s.index, s.start) for s in h.segments] == [(1, 60.0)]
    h.answers[BILI] = LIVE
    h.tick()
    loaded = Session.load(session.dir)
    assert [p.index for p in loaded.parts] == [1, 2]
    assert epoch(loaded.parts[0].ended_at) == T0 + 120


def test_recovery_ends_an_offline_room(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    session = crashed_session(h)
    h.now = T0 + 5000
    h.watcher.recover()
    assert len(h.segments) == 2
    h.tick()
    assert h.ended == [session.dir]
    assert h.resolved == [BILI] or h.resolved == []


def test_recovery_ends_sessions_of_removed_rooms(tmp_path):
    h = Harness(tmp_path, [])
    session = crashed_session(h)
    h.watcher.recover()
    assert h.ended == [session.dir]
    assert Session.load(session.dir).state == STATE_ENDED


def test_disabling_a_room_ends_its_session(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = LIVE
    h.tick()
    capture = FakeCapture.instances[0]
    save_rooms(h.rooms_path, [Room(BILI, "bilibili", enabled=False)])
    h.tick(T0 + 10)
    assert capture.stopped
    assert h.ended == [h.session().dir]
    assert h.session().state == STATE_ENDED


THREE = StreamInfo(
    True,
    "bilibili",
    "主播",
    "讲座",
    urls=("https://a.cdn/x.flv", "https://b.cdn/x.flv", "https://c.cdn/x.m3u8"),
)


def fail_fast(capture, seconds=5.0, code=1):
    capture.add_segment(0, 0.0, seconds)
    capture.exited, capture.exit_code = True, code
    capture.error = "HTTP error 403 Forbidden"


def hosts():
    return [c.info.url.split("/")[2] for c in FakeCapture.instances]


def test_host_that_fails_fast_is_avoided_then_retried_after_expiry(tmp_path, caplog):
    caplog.set_level("INFO", logger="livedigest.watch")
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = THREE
    h.tick()
    assert hosts() == ["a.cdn"]
    assert THREE.url == "https://a.cdn/x.flv"
    fail_fast(FakeCapture.instances[0])
    h.tick(T0 + 10)
    assert "a.cdn ended after 5 s of audio (exit 1, host penalised)" in caplog.text
    assert "403 Forbidden" in caplog.text
    h.tick(T0 + 12)  # the next candidate is tried within seconds
    h.tick(T0 + 14)
    assert hosts() == ["a.cdn", "b.cdn"]
    assert "avoiding a.cdn" in caplog.text

    # A healthy part ending does not penalise its host; a.cdn stays avoided.
    second = FakeCapture.instances[1]
    second.add_segment(0, 0.0, 60.0)
    h.tick(T0 + 76)
    second.add_segment(1, 60.0, 300.0)
    second.exited = True
    h.tick(T0 + 316)
    h.tick(T0 + 320)
    assert hosts() == ["a.cdn", "b.cdn", "b.cdn"]

    # Once the penalty expires the first choice is used again.
    FakeCapture.instances[2].add_segment(0, 0.0, 60.0)
    h.tick(T0 + 400)
    FakeCapture.instances[2].exited = True
    h.tick(T0 + 700)
    h.tick(T0 + 704)
    assert hosts()[-1] == "a.cdn"


def test_every_host_failing_backs_off_and_retries_the_oldest(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")])
    h.answers[BILI] = THREE
    h.tick()
    now = T0
    for _ in range(3):
        fail_fast(FakeCapture.instances[-1], seconds=2.0)
        now += 4
        h.tick(now)  # part ends
        h.tick(now + 4)  # next candidate, if any is left, starts promptly
    assert hosts() == ["a.cdn", "b.cdn", "c.cdn"]
    # c.cdn failing exhausted the list: back off, then retry the oldest failure.
    h.tick(now + watch.RESTART_DELAY - 1)
    assert len(FakeCapture.instances) == 3
    h.tick(now + watch.RESTART_DELAY)
    assert hosts()[-1] == "a.cdn"
    now += watch.RESTART_DELAY
    fail_fast(FakeCapture.instances[-1], seconds=2.0)
    h.tick(now + 2)
    h.tick(now + 2 + watch.RESTART_DELAY)
    assert len(FakeCapture.instances) == 4  # the delay doubled
    h.tick(now + 2 + watch.RESTART_DELAY * 2)
    assert hosts()[-1] == "b.cdn"


def test_parts_stopped_by_the_watcher_are_not_penalised(tmp_path):
    h = Harness(tmp_path, [Room(BILI, "bilibili")], free=50.0)
    h.answers[BILI] = THREE
    h.tick()
    h.free = 0.5
    h.tick(T0 + 5)
    h.free = 50.0
    h.tick(T0 + 10)
    h.tick(T0 + 70)
    assert hosts() == ["a.cdn", "a.cdn"]
