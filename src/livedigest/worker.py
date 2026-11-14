"""Transcribe closed audio segments one at a time, then finish each broadcast.

One transcription thread and one queue serve every room, so only one model
is ever in memory however many rooms are live. Polish and digest call an
external command for minutes per broadcast and need no model, so they run
on a second thread instead of holding up live segments.

Everything is resumable from disk: a segment is done once its
``transcripts/pNN_SSS.json`` exists, and a broadcast once its job records
``finalized_at``; ``scan`` queues whatever is missing.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path

from livedigest import pipeline, polish
from livedigest.config import Settings
from livedigest.engines import Engine, EngineError, get_engine
from livedigest.files import write_json
from livedigest.session import (
    STATE_ENDED,
    SegmentRef,
    Session,
    find_sessions,
    segment_refs,
    transcript_path,
)
from livedigest.transcript import Segment

log = logging.getLogger(__name__)

POLL_SECONDS = 0.5


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: Engine | None = None,
        *,
        retry_failed: bool = False,
    ) -> None:
        self.settings = settings
        self.retry_failed = retry_failed
        self._engine = engine
        self._segments: queue.Queue[SegmentRef | Path] = queue.Queue()
        self._finals: queue.Queue[Path] = queue.Queue()
        self._queued: set[Path] = set()
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self.transcribed = 0
        self.failed = 0

    # -- callbacks (any thread) ----------------------------------------------

    def on_segment(self, ref: SegmentRef) -> None:
        if self._claim(ref.path):
            self._segments.put(ref)

    def on_session_end(self, session_dir: Path) -> None:
        """Queued behind the broadcast's segments, so it sees all of them."""
        self._segments.put(session_dir)

    def _claim(self, key: Path) -> bool:
        with self._guard:
            if key in self._queued:
                return False
            self._queued.add(key)
            return True

    def scan(self) -> int:
        """Queue segments without a transcript and broadcasts left unfinished.

        A segment whose transcription failed earlier is retried here, once
        per start, never in a loop.
        """
        queued = 0
        for session in find_sessions(self.settings.recordings_dir):
            missing = [
                ref
                for ref in segment_refs(session)
                if not transcript_path(ref).exists()
            ]
            for ref in missing:
                self.on_segment(ref)
            queued += len(missing)
            if session.state == STATE_ENDED and (
                missing
                or pipeline.needs_finalize(self.settings, session, self.retry_failed)
            ):
                self.on_session_end(session.dir)
        return queued

    # -- running ----------------------------------------------------------------

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = get_engine(self.settings.transcribe)
        return self._engine

    def start(self) -> None:
        for name, target in (("transcribe", self._loop), ("finish", self._finish)):
            # Daemon: a second Ctrl-C must still end the process; every output
            # is written atomically and resumes on the next start.
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float | None = None) -> None:
        """Let the current segment finish, then exit; queued work stays on disk.

        A polish or digest in progress is not waited for (it can take
        minutes); its broadcast is finished again on the next start.
        """
        self._stopping.set()
        if not self._threads:
            return
        transcriber, finisher = self._threads
        transcriber.join(timeout)
        finisher.join(POLL_SECONDS * 2)
        if finisher.is_alive() or not self._finals.empty():
            log.info("unfinished polish/digest work resumes on the next start")

    def drain(self) -> None:
        """Do all queued work in the calling thread (``livedigest finish``)."""
        while True:
            try:
                item = self._segments.get_nowait()
            except queue.Empty:
                break
            self._handle(item, inline=True)
        while not self._finals.empty():
            self._guarded(self._finalize, self._finals.get_nowait())

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._segments.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            self._handle(item, inline=False)

    def _finish(self) -> None:
        while not self._stopping.is_set():
            try:
                session_dir = self._finals.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            self._guarded(self._finalize, session_dir)

    def _handle(self, item: SegmentRef | Path, inline: bool) -> None:
        if isinstance(item, SegmentRef):
            self._guarded(self._transcribe, item)
            with self._guard:
                self._queued.discard(item.path)
        elif inline:
            self._guarded(self._finalize, item)
        else:
            self._finals.put(item)

    @staticmethod
    def _guarded(action, argument) -> None:
        try:
            action(argument)
        # One bad segment or broadcast must not stop the others.
        except Exception:  # noqa: BLE001
            log.exception("%s failed for %s", action.__name__, argument)

    # -- work -------------------------------------------------------------------

    def _transcribe(self, ref: SegmentRef) -> None:
        out = transcript_path(ref)
        if not out.exists():
            session = Session.load(ref.session_dir)
            hotwords = polish.load_glossary(self.settings.glossary_dir, session.anchor)
            started = time.monotonic()
            try:
                lines = self.engine.transcribe(ref.path, hotwords)
            # Any engine failure (a crash in a model library included) is
            # recorded, and the segment, still without a transcript, is
            # retried on the next start.
            except Exception as exc:  # noqa: BLE001
                if not isinstance(exc, EngineError):
                    log.exception("engine crashed on %s", ref.path)
                    exc = EngineError(f"{type(exc).__name__}: {exc}")
                self.failed += 1
                pipeline.record_failure(self.settings, "transcribe", ref.path, str(exc))
                return
            seconds = time.monotonic() - started
            write_segment(out, ref, self.engine.name, lines, seconds)
            self.transcribed += 1
            log.info(
                "%s %s: %d lines, %.0f s of audio in %.1f s",
                session.id, ref.path.name, len(lines), ref.end - ref.start, seconds,
            )  # fmt: skip
        if not pipeline.refresh_session(self.settings, ref.session_dir):
            # Being finalized right now: finalize again so this one is included.
            self._finals.put(ref.session_dir)

    def _finalize(self, session_dir: Path) -> None:
        result = pipeline.finalize_session(
            self.settings, session_dir, retry_failed=self.retry_failed
        )
        states = {k: v.get("state") for k, v in result.status["stages"].items()}
        log.info("finished %s: %s", result.job_dir, states)


def write_segment(
    out: Path, ref: SegmentRef, engine: str, lines: list[Segment], seconds: float
) -> None:
    """Write one segment's lines with times from session start, atomically."""
    shifted = [
        Segment(
            round(line.start + ref.start, 3),
            round(line.end + ref.start, 3),
            line.text,
            line.speaker,
        )
        for line in lines
    ]
    payload = {
        "start": ref.start,
        "end": ref.end,
        "engine": engine,
        "seconds": round(seconds, 1),
        "segments": [asdict(line) for line in shifted],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, payload, indent=1)
