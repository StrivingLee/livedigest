import json

from livedigest import cli, rooms


def run(settings, monkeypatch, *args):
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    return cli.main(list(args))


def test_add_list_disable_enable(settings, monkeypatch, capsys):
    share = "【来看直播】 https://live.bilibili.com/1000001?broadcast_type=0 快来"
    assert (
        run(settings, monkeypatch, "add", share, "--keep", "audio", "--name", "课") == 0
    )
    assert "added: https://live.bilibili.com/1000001" in capsys.readouterr().out
    assert run(settings, monkeypatch, "add", share) == 0
    assert "already watched" in capsys.readouterr().out

    assert run(settings, monkeypatch, "disable", "课") == 0
    [room] = rooms.load_rooms(settings.rooms_path)
    assert not room.enabled and room.keep == "audio"
    assert run(settings, monkeypatch, "enable", share) == 0
    assert rooms.load_rooms(settings.rooms_path)[0].enabled
    assert run(settings, monkeypatch, "disable", "nothing") == 1

    capsys.readouterr()
    assert run(settings, monkeypatch, "rooms") == 0
    out = capsys.readouterr().out
    assert out.startswith("on ") and "keep=audio" in out and "课" in out


def test_status_reports_without_loading_a_model(settings, monkeypatch, capsys):
    assert run(settings, monkeypatch, "status") == 0
    info = json.loads(capsys.readouterr().out)
    assert info["rooms"] == 0 and info["live_sessions"] == []
    assert info["transcribe_engine"].startswith(("qwen3-asr/", "mlx-whisper/"))
    assert "ffmpeg" in info and "free_gb" in info
