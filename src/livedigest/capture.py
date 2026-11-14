"""One ffmpeg process per live room: segmented speech audio plus sparse stills.

Audio segments are MPEG-TS flushed packet by packet: after ``kill -9`` every
segment, including the one being written, still decodes and reports its exact
duration. ADTS survives too but its duration is a bitrate estimate (8% off in
testing), and MP4/M4A loses the open segment entirely (no moov atom).
"""

from __future__ import annotations

import csv
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from livedigest.files import probe_duration
from livedigest.resolve import BILIBILI_REFERER, StreamInfo

log = logging.getLogger(__name__)

AUDIO_EXT = "ts"
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile "
    "Safari/537.36"
)
PROTOCOLS = "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy"
# ffmpeg refuses to start when an optional ``0:v:0?`` map finds nothing.
NO_VIDEO_MARKER = "does not contain any stream"
RAW_FRAME_RE = re.compile(r"^\.raw_p(\d+)_(\d+)\.jpg$")
STOP_GRACE_SECONDS = 15


@dataclass
class CaptureSettings:
    segment_seconds: int = 300
    audio_bitrate: str = "48k"
    audio_rate: int = 16000
    frame_interval: int = 10
    frame_height: int = 720
    poll_seconds: int = 60
    merge_gap_seconds: int = 600
    min_free_gb: float = 5.0
    video_segment_seconds: int = 1800
    # "low" asks the platform for a smaller stream where it offers one (a
    # Douyin game room drops from 19 to 2.3 Mbit/s at 720p); speech and a
    # still every 10 s need no more. Rooms kept as video always get "best".
    quality: str = "low"
    cookies: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None

    def __post_init__(self) -> None:
        if self.quality not in ("low", "best"):
            raise ValueError(
                f'capture.quality must be "low" or "best", not {self.quality!r}'
            )


@dataclass(frozen=True)
class PartPaths:
    session_dir: Path
    part: int

    @property
    def audio_dir(self) -> Path:
        return self.session_dir / "audio"

    @property
    def frames_dir(self) -> Path:
        return self.session_dir / "frames"

    @property
    def video_dir(self) -> Path:
        return self.session_dir / "video"

    @property
    def prefix(self) -> str:
        return f"p{self.part:02d}"

    @property
    def audio_pattern(self) -> Path:
        return self.audio_dir / f"{self.prefix}_%03d.{AUDIO_EXT}"

    @property
    def segment_list(self) -> Path:
        return self.audio_dir / f".{self.prefix}_segments.csv"

    @property
    def frame_pattern(self) -> Path:
        # Dot-prefixed until visual.py keeps (and renames) or deletes them.
        return self.frames_dir / f".raw_{self.prefix}_%06d.jpg"

    @property
    def video_pattern(self) -> Path:
        return self.video_dir / f"{self.prefix}_%03d.ts"

    @property
    def log_path(self) -> Path:
        return self.session_dir / f"{self.prefix}.ffmpeg.log"

    def raw_frames(self) -> list[Path]:
        """Unprocessed stills of this part, oldest first."""
        found = []
        for path in self.frames_dir.glob(f".raw_{self.prefix}_*.jpg"):
            if match := RAW_FRAME_RE.match(path.name):
                found.append((int(match.group(2)), path))
        return [path for _, path in sorted(found)]


@dataclass(frozen=True)
class ClosedSegment:
    index: int
    path: Path
    # Seconds from the start of the part.
    start: float
    end: float


def build_command(
    info: StreamInfo,
    paths: PartPaths,
    settings: CaptureSettings,
    keep: str,
    frames: bool = True,
) -> list[str]:
    """The ffmpeg argv for one part. No ``-re``: CDNs stall throttled readers."""
    if not info.url:
        raise ValueError("stream is not live")
    headers = dict(info.headers)
    user_agent = MOBILE_UA
    for key in list(headers):
        if key.lower() == "user-agent":
            user_agent = headers.pop(key)
    hls = info.platform == "youtube" or ".m3u8" in info.url
    # Overseas HLS needs longer patience, as in the old recorder.
    rw_timeout, analyze, probe = (
        ("50000000", "40000000", "20000000")
        if hls
        else ("15000000", "20000000", "10000000")
    )
    want_frames = frames and keep != "audio"
    command = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-y"]
    command += [
        "-rw_timeout", rw_timeout,
        "-protocol_whitelist", PROTOCOLS,
        "-thread_queue_size", "1024",
        "-analyzeduration", analyze,
        "-probesize", probe,
        "-fflags", "+discardcorrupt",
    ]  # fmt: skip
    # HTTP-only options make ffmpeg refuse other inputs (e.g. a local file).
    if info.url.startswith(("http://", "https://")):
        command += ["-user_agent", user_agent]
        if info.platform == "bilibili":
            # Some Bilibili CDN hosts answer 403 without it; the rest ignore it.
            command += ["-referer", BILIBILI_REFERER]
        if settings.proxy:
            command += ["-http_proxy", settings.proxy]
        if headers:
            joined = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            command += ["-headers", joined]
    if want_frames:
        # Decode keyframes only; stream copies are not decoded, so unaffected.
        command += ["-skip_frame", "nokey"]
    command += ["-i", info.url]
    command += [
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(settings.audio_rate),
        "-c:a", "aac", "-b:a", settings.audio_bitrate,
        "-f", "segment", "-segment_time", str(settings.segment_seconds),
        "-segment_format", "mpegts",
        "-segment_format_options", "flush_packets=1",
        "-reset_timestamps", "1",
        "-segment_list", str(paths.segment_list),
        "-segment_list_type", "csv",
        str(paths.audio_pattern),
    ]  # fmt: skip
    if want_frames:
        height = settings.frame_height
        command += [
            "-map", "0:v:0?", "-an", "-sn", "-dn",
            "-vf", f"fps=1/{settings.frame_interval},scale=-2:min({height}\\,ih)",
            "-q:v", "5", "-frame_pts", "1", "-f", "image2",
            str(paths.frame_pattern),
        ]  # fmt: skip
    if keep == "video":
        command += [
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-f", "segment", "-segment_time", str(settings.video_segment_seconds),
            "-segment_format", "mpegts",
            "-segment_format_options", "flush_packets=1",
            "-reset_timestamps", "1",
            str(paths.video_pattern),
        ]  # fmt: skip
    return command


def read_segment_list(paths: PartPaths) -> list[ClosedSegment]:
    """Segments ffmpeg has closed; it appends a row only after closing one."""
    try:
        text = paths.segment_list.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    # A row being written has no newline yet.
    complete = text[: text.rfind("\n") + 1]
    segments = []
    for row in csv.reader(complete.splitlines()):
        if len(row) < 3:
            continue
        name, start, end = row[0], float(row[1]), float(row[2])
        index = int(Path(name).stem.rsplit("_", 1)[1])
        segments.append(ClosedSegment(index, paths.audio_dir / name, start, end))
    return segments


def tail_segments(paths: PartPaths, known: list[ClosedSegment]) -> list[ClosedSegment]:
    """Segments on disk that ffmpeg never listed: it was killed mid-segment.

    MPEG-TS stays readable, so the probed duration is used. Empty files (the
    process died before writing a packet) are removed. The tail is appended to
    the segment list, which ffmpeg no longer writes, so every segment's offset
    can later be read back from disk alone.
    """
    listed = {segment.index for segment in known}
    start = known[-1].end if known else 0.0
    tail = []
    for path in sorted(paths.audio_dir.glob(f"{paths.prefix}_*.{AUDIO_EXT}")):
        index = int(path.stem.rsplit("_", 1)[1])
        if index in listed:
            continue
        duration = probe_duration(path) if path.stat().st_size else None
        if not duration:
            path.unlink()
            continue
        tail.append(ClosedSegment(index, path, start, start + duration))
        start += duration
    if tail:
        try:
            existing = paths.segment_list.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        rows = "".join(f"{s.path.name},{s.start:.6f},{s.end:.6f}\n" for s in tail)
        lead = "\n" if existing and not existing.endswith("\n") else ""
        with paths.segment_list.open("a", encoding="utf-8") as handle:
            handle.write(lead + rows)
    return tail


class Capture:
    """Owns the ffmpeg subprocess for one part."""

    def __init__(
        self,
        info: StreamInfo,
        paths: PartPaths,
        settings: CaptureSettings,
        keep: str,
    ) -> None:
        self.info = info
        self.paths = paths
        self.settings = settings
        self.keep = keep
        self.frames = keep != "audio"
        self._reported = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._finished = False

    def start(self) -> None:
        command = build_command(
            self.info, self.paths, self.settings, self.keep, self.frames
        )
        for directory in (self.paths.audio_dir, self.paths.frames_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if self.keep == "video":
            self.paths.video_dir.mkdir(parents=True, exist_ok=True)
        with self.paths.log_path.open("ab") as log_file:
            # Own process group: a terminal Ctrl-C must not reach ffmpeg before
            # the watcher has stopped it cleanly.
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
            )
        log.info("ffmpeg %s started (pid %d)", self.paths.prefix, self._process.pid)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process else None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _lacks_video(self) -> bool:
        try:
            text = self.paths.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return False
        return NO_VIDEO_MARKER in text

    def last_error(self) -> str:
        """The last line ffmpeg logged for this part, usually why it ended."""
        try:
            text = self.paths.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def poll_closed_segments(self) -> list[ClosedSegment]:
        """Newly closed segments; once ffmpeg has exited, also the unlisted tail."""
        if self._process is None or self._finished:
            return []
        exited = self._process.poll() is not None
        if exited and self.frames and self._lacks_video():
            log.info("%s has no video track; capturing audio only", self.info.anchor)
            self.frames = False
            self.start()
            return []
        segments = read_segment_list(self.paths)
        if exited:
            segments += tail_segments(self.paths, segments)
            self._finished = True
        new = segments[self._reported :]
        self._reported = len(segments)
        return new

    @property
    def finished(self) -> bool:
        """ffmpeg has exited and every segment it wrote has been returned."""
        return self._finished

    def stop(self, timeout: float = STOP_GRACE_SECONDS) -> int | None:
        """``q`` lets ffmpeg close the open segment and list it; signals are
        the fallback when it does not respond."""
        process = self._process
        if process is None or process.poll() is not None:
            return self.returncode
        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        for action in (None, process.terminate, process.kill):
            if action:
                action()
            try:
                return process.wait(timeout)
            except subprocess.TimeoutExpired:
                continue
        return process.poll()
