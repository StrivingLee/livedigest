from livedigest import slides


def test_embed_puts_each_still_before_the_first_later_line():
    markdown = "# t\n\n- anchor: a\n\n[00:00:05] 一\n[00:00:20] 二\n[00:01:00] 三\n"
    stills = [(0, "frames/p01_000000.jpg"), (20, "frames/p01_000020.jpg")]
    lines = slides.embed(markdown, stills).split("\n")
    assert lines.index("![00:00:00](frames/p01_000000.jpg)") < lines.index(
        "[00:00:05] 一"
    )
    image = lines.index("![00:00:20](frames/p01_000020.jpg)")
    assert lines.index("[00:00:05] 一") < image < lines.index("[00:00:20] 二")


def test_late_stills_go_after_the_last_paragraph_before_notes():
    markdown = "**[00:00:01]** 甲\n\n## 待核对\n\n- [00:00:01] 乙\n"
    out = slides.embed(markdown, [(90, "frames/p01_000090.jpg")])
    assert out.index("**[00:00:01]** 甲") < out.index("frames/p01_000090.jpg")
    assert out.index("frames/p01_000090.jpg") < out.index("## 待核对")


def test_copy_stills_skips_raw_frames_and_only_slides_count(tmp_path):
    frames = tmp_path / "session" / "frames"
    frames.mkdir(parents=True)
    for name in ("p01_000030.jpg", "p02_000000.jpg", ".raw_p01_000004.jpg"):
        (frames / name).write_bytes(b"jpg")
    job = tmp_path / "job"
    assert slides.copy_stills(frames, job) == [
        (0, "frames/p02_000000.jpg"),
        (30, "frames/p01_000030.jpg"),
    ]
    assert sorted(p.name for p in (job / "frames").iterdir()) == [
        "p01_000030.jpg",
        "p02_000000.jpg",
    ]
    assert slides.wanted({"mode": "slides"})
    assert not slides.wanted({"mode": "camera"}) and not slides.wanted({})
