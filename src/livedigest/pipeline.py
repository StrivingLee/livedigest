"""Transcript → polish → digest, one idempotent job per broadcast or file.

A captured broadcast's job lives in ``<output>/<date>/<anchor>/<session stamp>/``,
a local media file's in ``<output>/<date>/<anchor>/<file stem>/``:

- ``meta.json``         source, anchor, times, duration, engine
- ``transcript.json``   timed segments (the source of truth)
- ``transcript.srt``    subtitles for checking against the recording
- ``transcript.md``     timestamped raw transcript (grows while live)
- ``polished.json``     paragraph blocks: raw text, clean text, warnings
- ``transcript.clean.md`` reading version: filler removed, errors fixed
- ``digest.md``         short summary with timestamps
- ``frames/``           slide stills embedded in the transcripts (slides only)
- ``status.json``       per-stage state, so reruns skip finished work

Failures are also appended to ``<output>/failures.jsonl`` with their stage
(``capture``, ``transcribe``, ``polish`` or ``digest``) so capture and ASR
problems can be told apart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from livedigest import polish, recording, slides, speakers, transcript
from livedigest.config import Settings
from livedigest.digest import DigestError, write_digest
from livedigest.engines import EngineError, get_engine
from livedigest.files import file_lock, probe_duration, read_json, safe_name, write_json
from livedigest.session import (
    STATE_ENDED,
    STATE_LIVE,
    SegmentRef,
    Session,
    epoch,
    segment_refs,
    transcript_path,
)
from livedigest.transcript import Segment

log = logging.getLogger(__name__)

STAGES = ("transcribe", "polish", "digest")


@dataclass
class JobResult:
    job_dir: Path
    status: dict
    skipped: bool = False


def record_failure(settings: Settings, stage: str, source: Path, error: str) -> None:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "recording": str(source),
        "error": error,
    }
    with (settings.output_dir / "failures.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.error("%s failed for %s: %s", stage, source, error)


def _local(text: str | None) -> str | None:
    """Session times carry a zone; job metadata uses naive local seconds."""
    if not text:
        return None
    moment = datetime.fromtimestamp(epoch(text))
    return moment.isoformat(timespec="seconds")


def job_dir_for(settings: Settings, media: Path, anchor: str) -> Path:
    when = (
        recording.parse_name(media).started_at
        or _published(media)
        or datetime.fromtimestamp(media.stat().st_mtime)
    )
    day = when.date().isoformat()
    return settings.output_dir / day / safe_name(anchor) / safe_name(media.stem, 200)


def session_job_dir(settings: Settings, session: Session) -> Path:
    day = _local(session.started_at)[:10]
    return settings.output_dir / day / safe_name(session.anchor) / session.dir.name


def _published(media: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(recording.read_info(media)["published_at"])
    except (KeyError, TypeError, ValueError):
        return None


def _title(meta: dict, fallback: str) -> str:
    return f"{meta['anchor']} · {meta.get('started_at') or fallback}"


def _write_transcript(
    job_dir: Path,
    segments: list[Segment],
    title: str,
    meta: dict,
    stills: list[slides.Still],
) -> None:
    transcript.save_json(segments, job_dir / "transcript.json")
    (job_dir / "transcript.srt").write_text(transcript.to_srt(segments), "utf-8")
    markdown = transcript.to_markdown(segments, title, meta)
    (job_dir / "transcript.md").write_text(slides.embed(markdown, stills), "utf-8")


# -- local media files --------------------------------------------------------


def build_meta(media: Path, anchor: str, duration: float, fp: str) -> dict:
    info = recording.parse_name(media)
    started = info.started_at
    video = recording.read_info(media)
    platform = media.parent.parent.name if media.parent.parent else None
    meta = {
        "anchor": anchor,
        "platform": platform,
        "kind": "video" if platform == recording.VIDEO_FOLDER else "live",
        "source_url": video.get("source_url"),
        "started_at": started.isoformat(timespec="seconds") if started else None,
        "ended_at": (
            (started + timedelta(seconds=duration)).isoformat(timespec="seconds")
            if started
            else None
        ),
        "duration": transcript.stamp(duration),
        "duration_seconds": round(duration, 3),
        "segment": info.segment,
        "recording": str(media),
        "fingerprint": fp,
    }
    for key in ("title", "published_at"):
        if video.get(key):
            meta[key] = video[key]
    return meta


def process(
    settings: Settings,
    media: Path,
    *,
    anchor: str | None = None,
    retry_failed: bool = True,
    redo: str | None = None,
) -> JobResult:
    """Run every unfinished stage for one finished media file.

    ``redo`` names a stage to run again together with every stage after it.
    """
    media = media.resolve()
    anchor = anchor or recording.parse_name(media).anchor
    job_dir = job_dir_for(settings, media, anchor)
    with file_lock(job_dir / ".lock") as acquired:
        if not acquired:
            log.info("already being processed: %s", job_dir)
            return JobResult(job_dir, read_json(job_dir / "status.json"), True)
        result = _process_locked(settings, media, anchor, job_dir, retry_failed, redo)
    write_index(settings)
    return result


def _process_locked(
    settings: Settings,
    media: Path,
    anchor: str,
    job_dir: Path,
    retry_failed: bool,
    redo: str | None,
) -> JobResult:
    fp = recording.fingerprint(media)
    status_path = job_dir / "status.json"
    status = read_json(status_path)
    if status.get("fingerprint") != fp:
        status = {"fingerprint": fp, "stages": {}}
    stages = status["stages"]
    _forget(stages, redo)
    if not any(_runnable(stages, stage, retry_failed) for stage in STAGES):
        return JobResult(job_dir, status, skipped=True)

    duration = probe_duration(media)
    if duration is None:
        error = "ffprobe cannot read the recording"
        record_failure(settings, "capture", media, error)
        stages["capture"] = {"state": "failed", "error": error}
        write_json(status_path, status)
        return JobResult(job_dir, status)
    stages["capture"] = {"state": "done"}

    meta = build_meta(media, anchor, duration, fp)
    write_json(job_dir / "meta.json", meta)
    title = _title(meta, media.stem)
    glossary = polish.load_glossary(settings.glossary_dir, anchor)

    segments: list[Segment] | None = None
    if _runnable(stages, "transcribe", retry_failed):
        started = time.monotonic()
        log.info("transcribing %s", media)
        try:
            engine = get_engine(settings.transcribe)
            segments = speakers.unify(media, engine.transcribe(media, glossary))
        except EngineError as exc:
            stages["transcribe"] = {"state": "failed", "error": str(exc)}
            record_failure(settings, "transcribe", media, str(exc))
        else:
            meta["engine"] = engine.name
            write_json(job_dir / "meta.json", meta)
            _write_transcript(job_dir, segments, title, meta, [])
            stages["transcribe"] = {
                "state": "done",
                "engine": engine.name,
                "segments": len(segments),
                "seconds": round(time.monotonic() - started, 1),
            }
            if not segments:
                stages["transcribe"]["warning"] = "no speech recognised"
        write_json(status_path, status)
    elif (job_dir / "transcript.json").exists():
        segments = transcript.load_json(job_dir / "transcript.json")
        meta["engine"] = stages.get("transcribe", {}).get("engine")

    _later_stages(
        settings, job_dir, status, meta, title, segments, glossary, media, retry_failed
    )
    return JobResult(job_dir, status)


# -- captured broadcasts -------------------------------------------------------


def _merge(refs: list[SegmentRef]) -> tuple[list[Segment], list[str], int]:
    """Lines of every transcribed audio segment, the engines that made them,
    and how many audio segments have no transcript (yet)."""
    segments: list[Segment] = []
    engines: list[str] = []
    missing = 0
    for ref in refs:
        path = transcript_path(ref)
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            missing += 1
            continue
        segments += [Segment(**item) for item in data["segments"]]
        if data.get("engine") and data["engine"] not in engines:
            engines.append(data["engine"])
    return segments, engines, missing


def session_meta(session: Session, refs: list[SegmentRef], engines: list[str]) -> dict:
    """Job metadata from ``session.json``; times are local, without a zone."""
    if session.ended_at:
        duration = epoch(session.ended_at) - epoch(session.started_at)
    else:
        duration = refs[-1].end if refs else 0.0
    visual = session.visual
    return {
        "anchor": session.anchor,
        "platform": session.platform,
        "kind": "live",
        "title": session.title,
        "source_url": session.room_url,
        "started_at": _local(session.started_at),
        "ended_at": _local(session.ended_at),
        "duration": transcript.stamp(duration),
        "duration_seconds": round(duration, 3),
        "state": session.state,
        "parts": len(session.parts),
        "segments": len(refs),
        "engine": " + ".join(engines) or None,
        "visual": {
            key: visual[key]
            for key in ("mode", "slides_windows", "camera_windows", "kept_frames")
            if key in visual
        },
        "recording": str(session.dir),
    }


def _session_transcript(
    session: Session, job_dir: Path
) -> tuple[list[Segment], dict, list[slides.Still], int]:
    """Write the merged transcript; also returns how many segments lack one."""
    refs = segment_refs(session)
    segments, engines, missing = _merge(refs)
    meta = session_meta(session, refs, engines)
    stills = (
        slides.copy_stills(session.frames_dir, job_dir)
        if slides.wanted(session.visual)
        else []
    )
    write_json(job_dir / "meta.json", meta)
    _write_transcript(job_dir, segments, _title(meta, session.id), meta, stills)
    return segments, meta, stills, missing


def _transcribe_stage(state: str, meta: dict, segments: list[Segment]) -> dict:
    return {
        "state": state,
        "engine": meta["engine"],
        "segments": len(segments),
        "audio_segments": meta["segments"],
    }


def refresh_session(settings: Settings, session_dir: Path) -> bool:
    """Rewrite a broadcast's merged transcript after another segment.

    Returns False when the job is locked (being finalized); the caller should
    finalize again so the new segment is not left out.
    """
    session = Session.load(session_dir)
    job_dir = session_job_dir(settings, session)
    with file_lock(job_dir / ".lock") as acquired:
        if not acquired:
            return False
        segments, meta, _, _ = _session_transcript(session, job_dir)
        status_path = job_dir / "status.json"
        status = read_json(status_path) or {"stages": {}}
        state = "live" if session.state == STATE_LIVE else "partial"
        status["stages"]["transcribe"] = _transcribe_stage(state, meta, segments)
        status["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_json(status_path, status)
    write_index(settings)
    return True


def finalize_session(
    settings: Settings,
    session_dir: Path,
    *,
    retry_failed: bool = True,
) -> JobResult:
    """Merge an ended broadcast's segments, then polish, digest and index it.

    Polish and digest run again whenever the merged transcript changed, e.g.
    after a segment that failed earlier was transcribed on a later start.
    """
    session = Session.load(session_dir)
    job_dir = session_job_dir(settings, session)
    with file_lock(job_dir / ".lock", wait=True):
        segments, meta, stills, missing = _session_transcript(session, job_dir)
        title = _title(meta, session.id)
        status_path = job_dir / "status.json"
        status = read_json(status_path) or {"stages": {}}
        stages = status.setdefault("stages", {})
        digest = hashlib.sha256(
            json.dumps([asdict(s) for s in segments], ensure_ascii=False).encode()
        ).hexdigest()[:16]
        if status.get("transcript_hash") != digest:
            status["transcript_hash"] = digest
            _forget(stages, "polish")
        stages["transcribe"] = _transcribe_stage(
            "partial" if missing else "done", meta, segments
        )
        if missing:
            stages["transcribe"]["missing"] = missing
        if not segments:
            stages["transcribe"]["warning"] = "no speech recognised"
        write_json(status_path, status)
        glossary = polish.load_glossary(settings.glossary_dir, session.anchor)
        _later_stages(
            settings, job_dir, status, meta, title, segments, glossary,
            session_dir, retry_failed, stills,
        )  # fmt: skip
        status["finalized_at"] = status["updated_at"]
        write_json(status_path, status)
    write_index(settings)
    return JobResult(job_dir, status)


def needs_finalize(settings: Settings, session: Session, retry_failed: bool) -> bool:
    """An ended broadcast whose job never finished (or failed, when retrying)."""
    if session.state != STATE_ENDED:
        return False
    status = read_json(session_job_dir(settings, session) / "status.json")
    if not status.get("finalized_at"):
        return True
    stages = status.get("stages", {})
    return retry_failed and any(
        stages.get(stage, {}).get("state") in ("failed", "partial") for stage in STAGES
    )


# -- shared stages -------------------------------------------------------------


def _runnable(stages: dict, stage: str, retry_failed: bool) -> bool:
    state = stages.get(stage, {}).get("state")
    return state != "done" and (state != "failed" or retry_failed)


def _forget(stages: dict, redo: str | None) -> None:
    if redo:
        for stage in STAGES[STAGES.index(redo) :]:
            stages.pop(stage, None)


def _later_stages(
    settings: Settings,
    job_dir: Path,
    status: dict,
    meta: dict,
    title: str,
    segments: list[Segment] | None,
    glossary: list[str],
    source: Path,
    retry_failed: bool,
    stills: list[slides.Still] | tuple = (),
) -> None:
    """Polish and digest; each failure falls back without blocking the next."""
    stages = status["stages"]
    status_path = job_dir / "status.json"

    polishing = settings.polish.enabled and _runnable(stages, "polish", retry_failed)
    if segments and polishing:
        started = time.monotonic()
        log.info("polishing transcript for %s", source)
        try:
            blocks = polish.polish(
                segments,
                meta["anchor"],
                glossary,
                settings.polish,
                meta["kind"],
                meta.get("platform"),
            )
        except polish.PolishError as exc:
            stages["polish"] = {"state": "failed", "error": str(exc)}
            record_failure(settings, "polish", source, str(exc))
        else:
            polish.save_blocks(blocks, job_dir / "polished.json")
            markdown = polish.to_markdown(blocks, title, meta)
            (job_dir / "transcript.clean.md").write_text(
                slides.embed(markdown, stills), "utf-8"
            )
            stages["polish"] = {
                "state": "done",
                "blocks": len(blocks),
                "warnings": sum(1 for b in blocks if b.warning),
                "raw_chars": sum(len(b.raw) for b in blocks),
                "clean_chars": sum(len(b.text) for b in blocks),
                "seconds": round(time.monotonic() - started, 1),
            }
        write_json(status_path, status)

    digest_source = segments
    if stages.get("polish", {}).get("state") == "done":
        digest_source = polish.as_segments(
            polish.load_blocks(job_dir / "polished.json")
        )
    if (
        segments is not None
        and settings.digest.enabled
        and _runnable(stages, "digest", retry_failed)
    ):
        if not digest_source:
            stages["digest"] = {"state": "skipped", "reason": "empty transcript"}
        else:
            log.info("writing digest for %s", source)
            try:
                body = write_digest(digest_source, meta, settings.digest)
            except DigestError as exc:
                stages["digest"] = {"state": "failed", "error": str(exc)}
                record_failure(settings, "digest", source, str(exc))
            else:
                (job_dir / "digest.md").write_text(
                    _digest_page(title, meta, body), "utf-8"
                )
                stages["digest"] = {"state": "done"}
        write_json(status_path, status)

    status["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(status_path, status)


def _digest_page(title: str, meta: dict, body: str) -> str:
    facts = [
        f"- 主播: {meta['anchor']}",
        f"- 开始: {meta['started_at'] or '未知'}",
        f"- 时长: {meta['duration']}",
    ]
    if meta.get("title"):
        facts.insert(1, f"- 标题: {meta['title']}")
    if meta.get("source_url"):
        facts.append(f"- 来源: {meta['source_url']}")
    facts.append(f"- 录像: `{meta['recording']}`")
    facts.append("- 全文: [整理版](transcript.clean.md) · [原始转写](transcript.md)")
    return f"# {title}\n\n" + "\n".join(facts) + "\n\n" + body.strip() + "\n"


def write_index(settings: Settings) -> None:
    """Rebuild ``index.json`` and ``index.md`` over all jobs, newest first.

    Jobs written by earlier versions lack some keys; they are listed anyway.
    """
    rows = []
    for meta_path in settings.output_dir.glob("*/*/*/meta.json"):
        job_dir = meta_path.parent
        try:
            meta = read_json(meta_path)
            stages = read_json(job_dir / "status.json").get("stages", {})
        except ValueError:
            log.warning("unreadable job %s skipped in the index", job_dir)
            continue
        rows.append(
            {
                "job": str(job_dir.relative_to(settings.output_dir)),
                "anchor": meta.get("anchor"),
                "platform": meta.get("platform"),
                "title": meta.get("title"),
                "started_at": meta.get("started_at"),
                "duration": meta.get("duration"),
                "source_url": meta.get("source_url"),
                "engine": meta.get("engine"),
                "transcribe": stages.get("transcribe", {}).get("state", "pending"),
                "polish": stages.get("polish", {}).get("state", "pending"),
                "digest": stages.get("digest", {}).get("state", "pending"),
            }
        )
    rows.sort(key=lambda r: (r["started_at"] or "", r["job"]), reverse=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(settings.output_dir / "index.json", {"jobs": rows})

    lines = [
        "# Live digests",
        "",
        "| Started | Anchor | Duration | Transcript | Digest |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        job = row["job"]
        if row["polish"] == "done":
            transcript_cell = f"[clean]({job}/transcript.clean.md)"
        elif row["transcribe"] in ("done", "live", "partial"):
            label = "raw" if row["transcribe"] == "done" else row["transcribe"]
            transcript_cell = f"[{label}]({job}/transcript.md)"
        else:
            transcript_cell = row["transcribe"]
        digest_cell = (
            f"[{row['digest']}]({job}/digest.md)"
            if row["digest"] == "done"
            else row["digest"]
        )
        lines.append(
            f"| {row['started_at'] or '?'} | {row['anchor']} | {row['duration']} "
            f"| {transcript_cell} | {digest_cell} |"
        )
    (settings.output_dir / "index.md").write_text("\n".join(lines) + "\n", "utf-8")
