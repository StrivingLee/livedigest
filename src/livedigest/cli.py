"""Watch live rooms and turn each broadcast into a transcript and a digest."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from livedigest import douyin, pipeline, rooms
from livedigest.config import Settings, load_settings
from livedigest.engines import EngineError, get_engine
from livedigest.files import file_lock
from livedigest.session import STATE_LIVE, find_sessions
from livedigest.watch import Watcher
from livedigest.worker import Worker

log = logging.getLogger("livedigest")

WATCH_LOCK = ".watch.lock"


def _watch_lock(settings: Settings):
    """Held by ``watch``; ``finish`` must not end sessions a watcher is capturing."""
    return file_lock(settings.recordings_dir / WATCH_LOCK)


def _watcher(settings: Settings, worker: Worker) -> Watcher:
    return Watcher(
        settings.recordings_dir,
        settings.rooms_path,
        settings.capture,
        on_segment=worker.on_segment,
        on_session_end=worker.on_session_end,
    )


def cmd_add(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    try:
        room, changed = rooms.add_room(settings.rooms_path, ns.text, ns.keep, ns.name)
    except ValueError as exc:
        print(f"add: {exc}", file=sys.stderr)
        return 1
    print(("added: " if changed else "already watched: ") + room.url)
    return 0


def cmd_rooms(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    listed = rooms.load_rooms(settings.rooms_path)
    if not listed:
        print(f"no rooms in {settings.rooms_path}; add one with `livedigest add`")
    for room in listed:
        state = "on " if room.enabled else "off"
        name = f"  {room.name}" if room.name else ""
        print(f"{state}  {room.platform:<11} keep={room.keep:<5} {room.url}{name}")
    return 0


def _find_room(path: Path, text: str) -> rooms.Room | None:
    listed = rooms.load_rooms(path)
    for room in listed:
        if text in (room.url, room.name):
            return room
    try:
        _, url = rooms.normalize(text)
    except ValueError:
        return None
    return next((room for room in listed if room.url == url), None)


def cmd_enable(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    room = _find_room(settings.rooms_path, ns.room)
    if room is None:
        print(f"{ns.command}: no room matches {ns.room!r}", file=sys.stderr)
        return 1
    rooms.set_enabled(settings.rooms_path, room.url, ns.command == "enable")
    print(f"{ns.command}d: {room.url}")
    return 0


def cmd_watch(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    with _watch_lock(settings) as acquired:
        if not acquired:
            print("watch: another watcher is already running", file=sys.stderr)
            return 1
        worker = Worker(settings)
        queued = worker.scan()
        if queued:
            log.info("%d segment(s) left from earlier runs queued", queued)
        worker.start()
        watcher = _watcher(settings, worker)
        log.info("watching %s; Ctrl-C to stop", settings.rooms_path)
        try:
            # Installs SIGINT/SIGTERM handlers that stop the loop cleanly.
            watcher.run()
        finally:
            log.info("stopping: finishing the current segment")
            worker.stop()
    return 0


def cmd_finish(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    with _watch_lock(settings) as acquired:
        if not acquired:
            print("finish: `livedigest watch` is running and does this itself")
            return 1
        worker = Worker(settings, retry_failed=True)
        worker.scan()
        _watcher(settings, worker).recover(end_all=True)
        worker.drain()
    print(f"transcribed {worker.transcribed} segment(s), {worker.failed} failed")
    return 1 if worker.failed else 0


def cmd_process(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    status = 0
    for media in ns.files:
        result = pipeline.process(settings, Path(media), anchor=ns.anchor, redo=ns.redo)
        status |= _report(result)
    return status


def cmd_add_video(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    try:
        media = douyin.fetch_video(settings, ns.url)
    except douyin.DouyinError as exc:
        print(f"add-video: {exc}", file=sys.stderr)
        return 1
    print(f"saved: {media}")
    if ns.no_process:
        return 0
    return _report(pipeline.process(settings, media))


def cmd_status(ns: argparse.Namespace) -> int:
    settings = load_settings(ns.config)
    try:
        engine = get_engine(settings.transcribe).name
    except EngineError as exc:
        engine = f"error: {exc}"
    try:
        listed = rooms.load_rooms(settings.rooms_path)
    except rooms.RoomsError as exc:
        listed = []
        log.error("%s", exc)
    recordings = settings.recordings_dir
    live = find_sessions(recordings, STATE_LIVE) if recordings.exists() else []
    disk = recordings if recordings.exists() else recordings.parent
    info = {
        "recordings_dir": str(recordings),
        "output_dir": str(settings.output_dir),
        "rooms_path": str(settings.rooms_path),
        "rooms": len(listed),
        "rooms_enabled": sum(room.enabled for room in listed),
        "live_sessions": [session.id for session in live],
        "free_gb": round(shutil.disk_usage(disk).free / 1e9, 1),
        "transcribe_engine": engine,
        "polish_command": settings.polish.command if settings.polish.enabled else None,
        "digest_command": settings.digest.command if settings.digest.enabled else None,
        "cookies": sorted(settings.capture.cookies),
        "ffmpeg": shutil.which("ffmpeg"),
        "yt-dlp": shutil.which("yt-dlp"),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _report(result: pipeline.JobResult) -> int:
    stages = result.status.get("stages", {})
    states = {name: stage.get("state") for name, stage in stages.items()}
    print(f"{result.job_dir}: {json.dumps(states, ensure_ascii=False)}")
    return 1 if "failed" in states.values() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="livedigest", description=__doc__)
    parser.add_argument("--config", type=Path, help="path to livedigest.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="watch a live room (share text or link)")
    add.add_argument("text", help="room link, or the whole pasted share message")
    add.add_argument("--keep", choices=rooms.KEEP_MODES, default="auto")
    add.add_argument("--name", help="your own label for the room")
    add.set_defaults(func=cmd_add)

    sub.add_parser("rooms", help="list watched rooms").set_defaults(func=cmd_rooms)
    for name in ("enable", "disable"):
        toggle = sub.add_parser(name, help=f"{name} a room by link or name")
        toggle.add_argument("room")
        toggle.set_defaults(func=cmd_enable)

    sub.add_parser(
        "watch", help="capture live rooms and transcribe as they go"
    ).set_defaults(func=cmd_watch)
    sub.add_parser(
        "finish", help="transcribe and finish whatever is pending, without capturing"
    ).set_defaults(func=cmd_finish)

    proc = sub.add_parser("process", help="transcribe and digest local media files")
    proc.add_argument("files", nargs="+")
    proc.add_argument("--anchor", help="override the anchor parsed from the name")
    proc.add_argument(
        "--redo",
        choices=pipeline.STAGES,
        help="run this stage again, and every stage after it",
    )
    proc.set_defaults(func=cmd_process)

    video = sub.add_parser(
        "add-video", help="download a Douyin video, then transcribe and digest it"
    )
    video.add_argument("url", help="share link, video ID, or the whole share message")
    video.add_argument(
        "--no-process", action="store_true", help="only download the video"
    )
    video.set_defaults(func=cmd_add_video)

    sub.add_parser("status", help="show paths, engine, rooms and tools").set_defaults(
        func=cmd_status
    )

    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
