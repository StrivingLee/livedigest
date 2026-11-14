import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
from conftest import make_media

from livedigest import cli, douyin

DETAIL = {
    "aweme_id": "7000000000000000001",
    "desc": "示例标题？ #测试话题",
    "author": "☘️作者/ทดสอบ☘️",
    "author_sec_uid": "MS4w",
    "created": "2026-07-21 02:39:04",
    "duration_s": 54,
    "play_url": "https://v26-web.douyinvod.com/x/video/?a=6383",
    "images": [],
}


def opencli_run(*envelopes):
    """A fake ``subprocess.run`` answering each call with the next envelope."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        out = json.dumps(envelopes[len(calls) - 1], ensure_ascii=False)
        return subprocess.CompletedProcess(command, 0, out, "")

    run.calls = calls
    return run


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def opencli(tmp_path, settings):
    path = tmp_path / "bin" / "opencli"
    path.parent.mkdir()
    path.touch()
    settings.douyin.opencli = str(path)
    return path


@pytest.mark.parametrize(
    ("text", "target"),
    [
        (
            "7.94 复制打开抖音，看看【作者的作品】示例标题？ "
            "https://v.douyin.com/abc123/ a@b.cn 12/31 :0pm",
            "https://v.douyin.com/abc123/",
        ),
        (
            "https://www.douyin.com/video/7000000000000000001",
            "https://www.douyin.com/video/7000000000000000001",
        ),
        (" 7000000000000000001 ", "7000000000000000001"),
    ],
)
def test_extract_target(text, target):
    assert douyin.extract_target(text) == target


def test_extract_target_rejects_text_without_link():
    with pytest.raises(douyin.DouyinError):
        douyin.extract_target("没有链接")


def test_fetch_detail_retries_a_detached_bridge(opencli):
    run = opencli_run(
        {"ok": False, "error": {"code": "detached_mid_command", "message": "x"}},
        {"ok": True, "data": [DETAIL]},
    )
    assert douyin.fetch_detail("7000000000000000001", opencli, run) == DETAIL
    assert len(run.calls) == 2
    assert run.calls[0][1:4] == ["douyin", "detail", "7000000000000000001"]


def test_fetch_detail_explains_missing_login(opencli):
    run = opencli_run(
        {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "403"}},
    )
    with pytest.raises(douyin.DouyinError, match="log in to douyin.com"):
        douyin.fetch_detail("7000000000000000001", opencli, run)
    assert len(run.calls) == 1


def test_find_opencli_prefers_newest_nvm_node(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OPENCLI", raising=False)
    monkeypatch.setattr(douyin.shutil, "which", lambda name: None)
    for version in ("v9.11.0", "v26.12.1", "v22.3.0"):
        path = tmp_path / ".nvm/versions/node" / version / "bin" / "opencli"
        path.parent.mkdir(parents=True)
        path.touch()
    assert douyin.find_opencli().parts[-3] == "v26.12.1"


def test_download_sends_referer_and_reports_cdn_refusal(tmp_path):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return FakeResponse(b"mp4 bytes")

    dest = tmp_path / "v.mp4"
    douyin.download("https://cdn/v", dest, opener)
    assert dest.read_bytes() == b"mp4 bytes"
    assert seen[0].get_header("Referer") == "https://www.douyin.com/"

    def refuse(request, timeout):
        body = b"<p>Client_ip: 38.99.248.32</p>\n<p>Forbid_code: 020200</p>"
        raise urllib.error.HTTPError(request.full_url, 403, "", {}, io.BytesIO(body))

    with pytest.raises(douyin.DouyinError, match="Forbid_code 020200"):
        douyin.download("https://cdn/v", tmp_path / "w.mp4", refuse)
    assert not (tmp_path / "w.mp4.part").exists()


def test_fetch_video_saves_media_and_info(settings, opencli, tmp_path):
    clip = make_media(tmp_path / "clip.mp4").read_bytes()
    run = opencli_run({"ok": True, "data": [DETAIL]})
    media = douyin.fetch_video(
        settings,
        "https://v.douyin.com/abc123/",
        run,
        lambda request, timeout: FakeResponse(clip),
    )
    assert media == (
        settings.recordings_dir
        / "douyin-video"
        / "☘️作者 ทดสอบ☘️"
        / "示例标题？ #测试话题 [7000000000000000001].mp4"
    )
    info = json.loads(media.with_suffix(".info.json").read_text())
    assert info["published_at"] == "2026-07-21T02:39:04"
    assert info["source_url"] == "https://www.douyin.com/video/7000000000000000001"

    # A second run keeps the playable file instead of downloading again.
    def no_download(request, timeout):
        raise AssertionError("downloaded twice")

    again = douyin.fetch_video(
        settings,
        "7000000000000000001",
        opencli_run({"ok": True, "data": [DETAIL]}),
        no_download,
    )
    assert again == media


def test_fetch_video_keeps_the_video_when_info_fails(
    settings, opencli, tmp_path, monkeypatch
):
    def broken(detail):
        raise TypeError("odd detail")

    monkeypatch.setattr(douyin, "info_from_detail", broken)
    clip = make_media(tmp_path / "clip.mp4").read_bytes()
    media = douyin.fetch_video(
        settings,
        "7000000000000000001",
        opencli_run({"ok": True, "data": [DETAIL]}),
        lambda request, timeout: FakeResponse(clip),
    )
    assert media.exists()
    assert not media.with_suffix(".info.json").exists()


def test_fetch_video_rejects_image_posts(settings, opencli):
    run = opencli_run({"ok": True, "data": [{**DETAIL, "play_url": ""}]})
    with pytest.raises(douyin.DouyinError, match="image post"):
        douyin.fetch_video(settings, "7000000000000000001", run)


def test_add_video_reports_errors(settings, monkeypatch, capsys):
    def fail(settings, text):
        raise douyin.DouyinError("opencli AUTH_REQUIRED: 403")

    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(douyin, "fetch_video", fail)
    assert cli.main(["add-video", "7000000000000000001"]) == 1
    assert "AUTH_REQUIRED" in capsys.readouterr().err
