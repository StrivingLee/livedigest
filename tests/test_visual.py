import subprocess

import pytest

from livedigest import visual
from livedigest.session import Session
from livedigest.visual import classify, distance, hash_gray, process_window


def test_hash_gray_compares_horizontal_neighbours():
    rising = bytes(range(17)) * 16
    falling = bytes(reversed(range(17))) * 16
    assert hash_gray(rising) == 0
    assert hash_gray(falling) == (1 << 256) - 1
    assert distance(hash_gray(rising), hash_gray(falling)) == 256
    with pytest.raises(ValueError):
        hash_gray(b"short")


def test_classify_thresholds():
    assert classify([0, 5, 16, 40]) == ("slides", 0.75)
    assert classify([0, 50, 60, 85]) == ("camera", 0.25)
    assert classify([16, 17]) == ("slides", 0.5)
    assert classify([]) == ("camera", 0.0)


def test_dhash_of_a_real_jpeg(tmp_path):
    path = tmp_path / "gradient.jpg"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "gradients=s=340x320:c0=black:c1=white:x0=0:y0=0:x1=339:y1=0",
            "-frames:v", "1", str(path),
        ],
        check=True,
    )  # fmt: skip
    value = visual.dhash(path)
    assert value is not None and value.bit_count() < 16  # brighter to the right
    (tmp_path / "broken.jpg").write_bytes(b"not a jpeg")
    assert visual.dhash(tmp_path / "broken.jpg") is None


@pytest.fixture
def session(tmp_path):
    session = Session.create(tmp_path, "bilibili", "a", "", "u", "auto", 1e9)
    session.start_part(1e9)
    session.start_part(1e9 + 100)  # part 2 starts 100 s into the session
    return session


@pytest.fixture
def table(monkeypatch):
    """Fake dhash values keyed by file name."""
    values = {}
    monkeypatch.setattr(visual, "dhash", lambda path: values.get(path.name))
    return values


def raw_frames(session, part, hashes, table, start=0):
    """Empty files standing in for raw stills."""
    paths = []
    for number, value in enumerate(hashes, start):
        path = session.frames_dir / f".raw_p{part:02d}_{number:06d}.jpg"
        path.write_bytes(b"")
        table[path.name] = value
        paths.append(path)
    return paths


def flip(bits):
    return (1 << bits) - 1


def test_slides_keep_each_distinct_slide(session, table):
    slide_a, slide_b = 0, flip(60)
    hashes = [slide_a, slide_a | 1, slide_a, slide_b, slide_b | 3, slide_b]
    paths = raw_frames(session, 1, hashes, table)
    stats = process_window(session, 1, list(reversed(paths)))
    assert stats.mode == "slides" and stats.frames == 6
    assert [p.name for p in stats.kept] == ["p01_000000.jpg", "p01_000030.jpg"]
    assert sorted(p.name for p in session.frames_dir.iterdir()) == [
        "p01_000000.jpg",
        "p01_000030.jpg",
    ]
    assert (stats.start, stats.end) == (0, 50)


def test_camera_keeps_one_still_per_five_minutes(session, table):
    # Every still differs a lot from the previous one, as on a camera.
    hashes = [flip(64) << (64 * (n % 3)) for n in range(36)]
    paths = raw_frames(session, 2, hashes, table)
    stats = process_window(session, 2, paths[:30])  # 100 s .. 390 s
    assert stats.mode == "camera"
    assert [p.name for p in stats.kept] == ["p02_000100.jpg"]
    stats = process_window(session, 2, paths[30:])  # 400 s .. 450 s
    assert [p.name for p in stats.kept] == ["p02_000400.jpg"]
    visual_data = Session.load(session.dir).visual
    assert visual_data["kept_frames"] == 2 and visual_data["seen_frames"] == 36
    assert visual_data["mode"] == "camera"
    assert [w["mode"] for w in visual_data["windows"]] == ["camera", "camera"]


def test_decisions_span_windows(session, table):
    slide = flip(50)
    first = raw_frames(session, 1, [slide, slide, slide], table)
    process_window(session, 1, first)
    second = raw_frames(session, 1, [slide, slide | 1], table, start=3)
    stats = process_window(session, 1, second)
    # Same slide as the last kept one, so nothing new is kept.
    assert stats.mode == "slides" and stats.kept == []


def test_unreadable_stills_are_dropped(session, table):
    [path] = raw_frames(session, 1, [None], table)
    stats = process_window(session, 1, [path])
    assert stats.frames == 0 and not path.exists()
