from livedigest.config import load_settings


def test_sections_map_to_settings_and_cookies_come_from_their_own_file(tmp_path):
    config = tmp_path / "livedigest.toml"
    config.write_text(
        'rooms_path = "my-rooms.toml"\n'
        "[capture]\nsegment_seconds = 60\n"
        '[transcribe]\nengine = "whisper"\nlanguage = "en"\n'
        "[digest]\nenabled = false\n",
        "utf-8",
    )
    (tmp_path / "cookies.toml").write_text(
        'douyin = "ttwid=1"\nbilibili = ""\n', "utf-8"
    )
    settings = load_settings(config)
    assert settings.rooms_path == tmp_path / "my-rooms.toml"
    assert settings.recordings_dir == tmp_path / "recordings"
    assert settings.capture.segment_seconds == 60
    assert settings.capture.cookies == {"douyin": "ttwid=1"}
    assert (settings.transcribe.engine, settings.transcribe.language) == (
        "whisper",
        "en",
    )
    assert not settings.digest.enabled and settings.polish.enabled


def test_missing_files_give_defaults(tmp_path):
    settings = load_settings(tmp_path / "absent.toml")
    assert settings.output_dir == tmp_path / "digests"
    assert settings.capture.cookies == {}
    assert settings.transcribe.engine == "auto"
