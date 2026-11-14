"""Watch the rooms in ``rooms.toml`` and capture every live broadcast.

One loop, single-threaded: ffmpeg does the streaming in child processes and
the loop only resolves rooms, collects closed segments and prunes stills.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from livedigest import visual
from livedigest.capture import (
    Capture,
    CaptureSettings,
    ClosedSegment,
    PartPaths,
    read_segment_list,
    tail_segments,
)
from livedigest.resolve import ResolveError, StreamInfo, host_of, resolve
from livedigest.rooms import Room, load_rooms, set_enabled
from livedigest.session import (
    STATE_LIVE,
    SegmentRef,
    Session,
    epoch,
    find_sessions,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 2.0
# After ffmpeg exits the room is re-resolved this soon, so holes stay short.
RETRY_DELAY = 3
# When every candidate host has failed recently, back off from this delay,
# doubling up to the poll interval, so a dead room is not hammered.
RESTART_DELAY = 15
# A part that captured less audio than this (or exited with an error before
# ffmpeg closed a segment) died early; its host is avoided for a while.
FAST_FAIL_SECONDS = 60.0
PENALTY_SECONDS = 600
HEARTBEAT_SECONDS = 30
STOP_FREE_GB = 1.0  # below this, running captures are stopped
ORPHAN_GRACE_SECONDS = 10


@dataclass
class _Active:
    """A room with an open session, capturing or waiting to reconnect."""

    room: Room
    session: Session
    capture: Capture | None = None
    retry_at: float = 0.0
    saved_at: float = 0.0
    # The current part: its host, audio captured, and whether ffmpeg closed a
    # segment while still running.
    host: str = ""
    audio: float = 0.0
    closed_live: bool = False
    stopped: bool = False  # by the watcher (disk, disabled room, shutdown)
    candidates: tuple[str, ...] = ()
    exhausted: int = 0  # consecutive retries with every host penalised


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def _stop_orphan(pid: int) -> None:
    """End an ffmpeg left running by a crashed watcher, letting it close files."""
    try:
        name = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return
    if not name.endswith("ffmpeg"):
        return
    log.warning("stopping orphaned ffmpeg (pid %d)", pid)
    for sig in (signal.SIGINT, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + ORPHAN_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


class Watcher:
    def __init__(
        self,
        root: Path,
        rooms_path: Path,
        settings: CaptureSettings,
        on_segment: Callable[[SegmentRef], None] | None = None,
        on_session_end: Callable[[Path], None] | None = None,
        *,
        resolver: Callable[[Room, CaptureSettings], StreamInfo] = resolve,
        capture_factory: Callable[..., Capture] = Capture,
        clock: Callable[[], float] = time.time,
        free_gb: Callable[[Path], float] = _free_gb,
    ) -> None:
        self.root = root
        self.rooms_path = rooms_path
        self.settings = settings
        self.on_segment = on_segment
        self.on_session_end = on_session_end
        self._resolver = resolver
        self._capture_factory = capture_factory
        self._clock = clock
        self._free_gb = free_gb
        self._rooms: list[Room] = []
        self._active: dict[str, _Active] = {}
        self._next_check: dict[str, float] = {}
        # Room URL -> host -> when a part on that host last died early.
        self._penalties: dict[str, dict[str, float]] = {}
        self._stopping = threading.Event()
        self._low_disk = False

    # -- lifecycle --------------------------------------------------------

    def run(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_: self.stop())
        try:
            self.recover()
            while not self._stopping.is_set():
                self.tick()
                self._stopping.wait(TICK_SECONDS)
        finally:
            self.shutdown()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    def stop(self) -> None:
        self._stopping.set()

    def shutdown(self) -> None:
        """Stop captures cleanly but leave sessions live, so they resume."""
        for active in self._active.values():
            if active.capture:
                log.info("stopping capture of %s", active.session.id)
                active.stopped = True
                active.capture.stop()
                self._collect(active)
                self._close_part(active)

    def recover(self, end_all: bool = False) -> None:
        """Pick up sessions a crash (or a stopped watcher) left marked live.

        ``end_all`` ends every one of them instead of resuming, for finishing
        pending work without capturing; rooms are left as they are.
        """
        self._rooms = self._load_rooms()
        rooms = {room.url: room for room in self._rooms}
        for session in find_sessions(self.root, STATE_LIVE):
            part = session.current_part
            if part and part.ended_at is None:
                if part.pid:
                    _stop_orphan(part.pid)
                paths = PartPaths(session.dir, part.index)
                listed = read_segment_list(paths)
                segments = listed + tail_segments(paths, listed)
                # An orphaned ffmpeg may have outlived the last heartbeat.
                last = session.last_activity()
                if segments:
                    last = max(last, epoch(part.started_at) + segments[-1].end)
                self._handle_segments(
                    session, segments[part.segments :], final=True, live=False
                )
                session.touch(last)
                session.end_part(last)
                session.save()
            room = rooms.get(session.room_url)
            if end_all:
                log.info("ending %s without capturing", session.id)
                self._end_session(session, None)
                continue
            if room is None or not room.enabled:
                log.info("recovered %s; its room is gone, ending it", session.id)
                self._end_session(session, room)
                continue
            log.info("recovered %s; checking whether it is still live", session.id)
            self._active[room.url] = _Active(room, session, retry_at=self._clock())

    # -- one loop iteration -----------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        self._rooms = self._load_rooms()
        free = self._free_gb(self.root)
        if free < STOP_FREE_GB:
            for active in self._active.values():
                if active.capture and active.capture.running:
                    log.error("%.1f GB free: stopping %s", free, active.session.id)
                    active.stopped = True
                    active.capture.stop()
        enabled = {room.url for room in self._rooms if room.enabled}
        for active in list(self._active.values()):
            if active.room.url not in enabled and active.capture:
                if active.capture.running:
                    log.info("room %s disabled: stopping", active.room.url)
                active.stopped = True
                active.capture.stop()
            self._service(active, now, free, active.room.url in enabled)
        for room in self._rooms:
            if not room.enabled or room.url in self._active:
                continue
            if now < self._next_check.get(room.url, 0.0):
                continue
            self._next_check[room.url] = now + self.settings.poll_seconds
            if not self._disk_ok(free):
                continue
            info = self._resolve(room)
            if info and info.is_live:
                session = Session.create(
                    self.root,
                    room.platform,
                    info.anchor or room.name or "unknown",
                    info.title,
                    room.url,
                    room.keep,
                    now,
                )
                log.info("%s is live: new session %s", room.url, session.id)
                active = _Active(room, session)
                self._active[room.url] = active
                self._start_part(active, info, now)

    def _service(
        self, active: _Active, now: float, free: float, enabled: bool = True
    ) -> None:
        session = active.session
        if active.capture:
            self._collect(active)
            if active.capture.finished:
                delay = self._part_ended(active, now)
                self._close_part(active, now)
                active.retry_at = now + delay
            elif now - active.saved_at >= HEARTBEAT_SECONDS:
                session.touch(now)
                session.save()
                active.saved_at = now
            if active.capture or enabled:
                return
        if now < active.retry_at and enabled:
            return
        silent = now - session.last_activity()
        if silent >= self.settings.merge_gap_seconds or not enabled:
            reason = f"offline for {silent:.0f} s" if enabled else "room disabled"
            log.info("%s %s: session ended", session.id, reason)
            del self._active[active.room.url]
            self._next_check[active.room.url] = now + self.settings.poll_seconds
            self._end_session(session, active.room)
            return
        active.retry_at = now + min(
            self.settings.poll_seconds, self.settings.merge_gap_seconds - silent
        )
        if not self._disk_ok(free):
            return
        info = self._resolve(active.room)
        if info and info.is_live:
            log.info("%s is back: continuing %s", active.room.url, session.id)
            self._start_part(active, info, now)

    # -- parts and segments -----------------------------------------------

    def _penalised(self, room_url: str, now: float) -> dict[str, float]:
        """Hosts of this room whose part died early within the penalty window."""
        recent = {
            host: at
            for host, at in self._penalties.get(room_url, {}).items()
            if now - at < PENALTY_SECONDS
        }
        self._penalties[room_url] = recent
        return recent

    def _choose(
        self, room_url: str, urls: tuple[str, ...], now: float
    ) -> tuple[str, str]:
        """The first candidate whose host has not failed recently, and why.

        When every host has, the one that failed longest ago is retried.
        """
        penalised = self._penalised(room_url, now)
        fresh = [url for url in urls if host_of(url) not in penalised]
        if fresh:
            skipped = sorted({host_of(u) for u in urls[: urls.index(fresh[0])]})
            why = f"avoiding {', '.join(skipped)}" if skipped else "first choice"
            return fresh[0], why
        url = min(urls, key=lambda u: penalised[host_of(u)])
        return url, "every host failed recently; retrying the oldest failure"

    def _part_ended(self, active: _Active, now: float) -> float:
        """Log why the part ended, penalise its host if it died early, and
        return how long to wait before the next attempt."""
        capture = active.capture
        assert capture is not None
        code = capture.returncode
        early = not active.stopped and (
            active.audio < FAST_FAIL_SECONDS
            or (code not in (0, None) and not active.closed_live)
        )
        if early and active.host:
            self._penalties.setdefault(active.room.url, {})[active.host] = now
        penalised = self._penalised(active.room.url, now)
        fresh = [u for u in active.candidates if host_of(u) not in penalised]
        if not early:
            active.exhausted = 0
        if not early or fresh:
            delay = RETRY_DELAY
        else:
            active.exhausted += 1
            delay = min(
                RESTART_DELAY * 2 ** (active.exhausted - 1),
                max(RESTART_DELAY, self.settings.poll_seconds),
            )
        log.info(
            "%s p%02d on %s ended after %.0f s of audio (exit %s%s): %s; "
            "re-checking in %d s",
            active.session.id,
            active.session.current_part.index if active.session.current_part else 0,
            active.host or "?",
            active.audio,
            code,
            ", host penalised" if early and active.host else "",
            capture.last_error() or "no ffmpeg message",
            delay,
        )
        return delay

    def _start_part(self, active: _Active, info: StreamInfo, now: float) -> None:
        session = active.session
        url, why = self._choose(active.room.url, info.urls, now)
        info = replace(info, url=url)
        active.candidates = info.urls
        active.host = host_of(url)
        active.audio, active.closed_live, active.stopped = 0.0, False, False
        part = session.start_part(now)
        log.info(
            "%s p%02d: %s (candidate %d of %d, %s)",
            session.id, part.index, active.host,
            info.urls.index(url) + 1, len(info.urls), why,
        )  # fmt: skip
        capture = self._capture_factory(
            info, PartPaths(session.dir, part.index), self.settings, session.keep
        )
        try:
            capture.start()
        except OSError as exc:
            log.error("could not start ffmpeg for %s: %s", session.id, exc)
            session.end_part(now)
            session.save()
            active.retry_at = now + RESTART_DELAY
            return
        part.pid = capture.pid
        session.save()
        active.capture = capture
        active.saved_at = now

    def _collect(self, active: _Active) -> None:
        capture = active.capture
        if capture is None:
            return
        segments = capture.poll_closed_segments()
        if segments:
            active.audio = max(active.audio, segments[-1].end)
            if not capture.finished:
                active.closed_live = True
        if segments or capture.finished:
            self._handle_segments(active.session, segments, final=capture.finished)

    def _close_part(self, active: _Active, now: float | None = None) -> None:
        active.session.end_part(now if now is not None else self._clock())
        active.session.save()
        active.capture = None

    def _handle_segments(
        self,
        session: Session,
        segments: list[ClosedSegment],
        final: bool,
        live: bool = True,
    ) -> None:
        """Prune each segment's stills, then hand the segment on.

        A still belongs to the window of the segment its timestamp falls in;
        when the part has ended, the last window takes every leftover still.
        """
        part = session.current_part
        if part is None:
            return
        paths = PartPaths(session.dir, part.index)
        interval = self.settings.frame_interval
        raw = paths.raw_frames()
        for position, segment in enumerate(segments):
            last = final and position == len(segments) - 1
            window = [
                path
                for path in raw
                if last or visual.frame_time(path, 0, interval) < segment.end
            ]
            raw = [path for path in raw if path not in window]
            if window:
                stats = visual.process_window(session, part.index, window, interval)
                log.info(
                    "%s p%02d_%03d: %s, kept %d/%d stills",
                    session.id, part.index, segment.index,
                    stats.mode, len(stats.kept), stats.frames,
                )  # fmt: skip
            part.segments += 1
            if live:
                session.touch(self._clock())
            session.save()
            ref = SegmentRef(
                session.dir,
                part.index,
                segment.index,
                segment.path,
                round(part.offset_seconds + segment.start, 3),
                round(part.offset_seconds + segment.end, 3),
            )
            self._call(self.on_segment, ref)
        if final and raw:
            visual.process_window(session, part.index, raw, interval)

    def _end_session(self, session: Session, room: Room | None) -> None:
        session.end()
        session.save()
        self._call(self.on_session_end, session.dir)
        if room and room.single_use:
            set_enabled(self.rooms_path, room.url, False)
            log.info("disabled single-use Xiaohongshu link %s", room.url)

    # -- helpers ------------------------------------------------------------

    def _load_rooms(self) -> list[Room]:
        try:
            return load_rooms(self.rooms_path)
        except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError) as exc:
            log.error(
                "cannot read %s (%s); keeping the previous list", self.rooms_path, exc
            )
            return self._rooms

    def _resolve(self, room: Room) -> StreamInfo | None:
        try:
            return self._resolver(room, self.settings)
        except ResolveError as exc:
            log.warning("%s", exc)
            return None

    def _disk_ok(self, free: float) -> bool:
        low = free < self.settings.min_free_gb
        if low and not self._low_disk:
            log.warning("only %.1f GB free: not starting new captures", free)
        self._low_disk = low
        return not low

    @staticmethod
    def _call(callback: Callable | None, argument: object) -> None:
        if callback is None:
            return
        try:
            callback(argument)
        # A downstream failure must never stop the capture of live rooms.
        except Exception:  # noqa: BLE001
            log.exception("callback %r failed", callback)
