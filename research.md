# Design record: audio-first live capture

**摘要（中文）。** 本文记录 livedigest 现行设计的依据。目标是把多个平台的直播变成带时间戳的转写稿和摘要，同时尽量少占磁盘、少占内存。最终方案是每个直播间由自己的一个 ffmpeg 进程直接写出 16 kHz 单声道 AAC 的 5 分钟 MPEG-TS 音频分段和每 10 秒一张关键帧截图，视频默认不落盘；每个分段关闭后立即由单个工作线程用 Qwen3-ASR（mlx-audio）转写；下播后用命令行 LLM 整理并摘要。被否决的方案包括：继续使用第三方录制器的纯音频模式、分段录视频再抽取删除、依赖平台的纯音频流、以及不留文件的流式 ASR。文中数据均在一台 16 GB Apple Silicon Mac 上测得。末尾“History”一节保留了 2026 年 10 月早期调研中仍然成立的结论。

This is a working record of why livedigest is built the way it is. For setup and usage see [README.md](README.md); for credits see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md). All measurements were taken on a 16 GB Apple Silicon Mac.

## Goals and constraints

- Turn live broadcasts on Douyin, Bilibili, Xiaohongshu and YouTube into a timestamped transcript and a short digest, mostly Mandarin speech.
- Several rooms may be live at once, unattended, on one laptop.
- The speech is the content. The picture matters only for slide lectures and screen shares, and as occasional context otherwise.
- Disk and memory must stay bounded: a 1080p stream is about 2 GB/h as video.
- Survive crashes, network drops and restarts without losing what was already captured, and without transcribing anything twice.
- Everything inspectable and re-runnable from files on disk.

## Capture: options considered

| Option | Summary | Verdict |
|---|---|---|
| A. Third-party recorder in audio-only mode | Keep driving DouyinLiveRecorder, switched to m4a/mp3, and hand finished files to the pipeline through its post-record hook. | Rejected. No stills at all, so slide lectures lose half their content. One global config for every room. The hook fires only on a clean ffmpeg exit and races the recorder's own post-processing. |
| B. Record video in segments, extract, delete | Record the stream in short video segments, extract audio and stills from each closed segment, delete it. | Rejected. Bounded buffer, but writes gigabytes per hour only to delete them, and needs a second decode pass per segment. |
| **C. Own ffmpeg, video never written** | One ffmpeg process per live room reads the stream once and writes audio segments plus keyframe stills directly. | **Chosen.** |
| D. Platform audio-only renditions | Ask each platform for an audio-only stream. | Rejected. Not confirmed to exist on any of the four platforms, and it loses the stills. |
| E. Pipe into streaming ASR, no files | Feed decoded audio straight to a streaming recogniser. | Rejected. Nothing to re-run, audit or re-transcribe with a better model later. |

### How option C works

`capture.build_command` builds a single ffmpeg invocation with two or three outputs:

1. **Audio**: first audio track → mono, 16 kHz, AAC 48 kbit/s → `segment` muxer, 300 s segments in MPEG-TS with `flush_packets=1`, plus a CSV segment list. ffmpeg appends a row to the list only after it closes a segment, so the list is the signal that a segment is ready for transcription.
2. **Stills** (unless `keep = "audio"`): `-skip_frame nokey` on the input, so only keyframes are decoded, then `fps=1/10` and a scale to at most 720 px high, written as JPEG with dot-prefixed names until they are classified.
3. **Video** (only `keep = "video"`): a stream copy in 30-minute MPEG-TS segments.

There is no `-re`: a reader throttled to real time falls behind and CDNs stall it. Input options (read/write timeout, probe sizes, a mobile User-Agent, longer patience for HLS) follow what DouyinLiveRecorder learned per platform. If the stream has no video track, ffmpeg refuses the optional video map, and the capture restarts audio-only.

Measured: the single pass costs under 1% of a CPU core per stream; offline it runs 165× real time. A 1080p Douyin stream is ~2 GB/h as video versus ~30 MB/h of audio plus 1–30 MB/h of kept stills.

### Why MPEG-TS segments

Tested by killing ffmpeg with `kill -9` mid-segment:

| Container | Open segment after `kill -9` |
|---|---|
| **MPEG-TS** | Fully readable, exact duration. |
| ADTS (`.aac`) | Readable, but the duration is a bitrate estimate (about 8% off). |
| MP4 / M4A | Unreadable: the `moov` atom is written only at the end. |

On recovery, segments ffmpeg never listed are probed and appended to the segment list, so every segment's offset can be rebuilt from disk alone. Empty files are deleted. mlx-audio picks its decoder by file extension and cannot read `.ts`, so the engine decodes each segment with ffmpeg to float32 samples first.

## Stills: slides or camera

Each window of stills (the ones falling inside a closed audio segment) is hashed with a 256-bit difference hash (17×16 grey thumbnail, one bit per horizontal neighbour pair). Measured medians of the Hamming distance between stills 10 s apart:

| Stream type | Median distance |
|---|---|
| Slides lecture | 11/256 (85% of pairs ≤ 16; a jump > 40 at each new slide) |
| Talking head | 51 |
| Product camera | 85 |

So a window where at least half of consecutive pairs are within 16 bits is **slides**: a still is kept when it is more than 32 bits from the last kept one, i.e. once per distinct slide. Anything else is **camera**: one still per 5 minutes. Hash state carries across windows. A session that is mostly slides gets its kept stills copied next to the transcript and embedded before the line they precede; camera stills stay in the recording folder only.

## Sessions and parts

A broadcast is a **session** (`recordings/<platform>/<anchor>/<start>/session.json`). Each ffmpeg run is a **part**. When ffmpeg exits (CDN drop, URL expiry), the watcher re-resolves the room within seconds and, if it is live again, starts a new part in the same session. The resolver returns every candidate stream URL it can find, best first (for Bilibili, all the CDN hosts the room's play APIs list). A part that dies early marks its CDN host to be avoided for a while, so the next part starts on another host; when every host has failed recently, retries back off towards the poll interval. If the room stays offline for `merge_gap_seconds` (600 s) since its last sign of life, the session ends and is finalized. Ending time is the last activity, not the detection time.

ffmpeg runs in its own process group so a terminal Ctrl-C reaches the watcher first; the watcher sends `q` so ffmpeg closes and lists the open segment, falling back to SIGTERM and SIGKILL. A watcher crash leaves sessions marked live and records ffmpeg's pid; on the next start, recovery stops any orphaned ffmpeg, collects its tail segments and resumes or ends each session.

## Transcription timing

| Option | Verdict |
|---|---|
| After the stream ends | Simple, but the transcript arrives hours late and work piles up when several broadcasts end together. |
| **Per closed segment** | **Chosen.** A 5-minute segment is transcribed in well under a minute, so the transcript lags the stream by a few minutes; the batch model sees whole segments and keeps full accuracy; everything stays re-runnable. |
| True streaming ASR | Lower accuracy on partial context and much more machinery (VAD, overlap, revision of partial hypotheses), for latency nobody needs in a digest. |

One worker thread and one queue serve all rooms, so exactly one model is in memory regardless of how many rooms are live. Polish and digest run on a second thread because they take minutes per broadcast and need no model. A segment is done once `transcripts/pNN_SSS.json` exists; a broadcast once its job records `finalized_at`; on start-up `scan` queues whatever is missing, retrying a failed segment once per start.

Measured: Qwen3-ASR 1.7B 8-bit transcribes about 10× real time (a 50-minute recording in 292 s; a 60 s live segment in 5–7 s). With two rooms live the watcher's memory footprint stayed flat at about 2.7 GB (peak 3.9 GB).

## Speech engine

| Engine | Verdict |
|---|---|
| **Qwen3-ASR 1.7B via mlx-audio** | **Chosen.** Accepts hotwords (fed from the glossary), best published Mandarin accuracy among locally runnable options, Apache-2.0 weights, runs on MLX. |
| mlx-whisper (large-v3-turbo) | Kept as fallback. On 60 s of silence it invented a sign-off line where Qwen3-ASR returned nothing; Whisper's own no-speech rule and a list of known hallucinations are applied. |
| FunASR / SenseVoice / Paraformer | Not adopted: PyTorch, and MPS acceleration unverified. |
| sherpa-onnx | No PyTorch, but only the 0.6B Qwen3-ASR export was available. |
| FireRedASR2 | Too heavy for a laptop that is also capturing. |
| BaoCut CLI (earlier engine) | Dropped: depends on a desktop app being installed, and its model packaging is not loadable by standard runtimes. |

Qwen3-ASR is run in 20-second chunks so lines carry real timestamps (with the library's default chunk a whole segment comes back as one timestamp). Within a chunk, sentence boundaries are interpolated by character count. Repetition loops are collapsed, lone fillers over long silence and implausibly dense output are dropped.

## Polish and digest

The merged transcript is rewritten into paragraphs by a command-line LLM (`claude -p` by default; any command reading a prompt on stdin works). The model returns paragraphs that name the segment id they start at, so each paragraph keeps an exact timestamp. Safety checks replace a paragraph with its raw text when the rewrite is longer than the source or cuts a long span below 25%, and changed numbers are flagged. The digest is written from the polished text. Both are re-run automatically when the merged transcript changes (for example after a previously failed segment is transcribed).

## History

The project began in October 2026 as a Xiaohongshu-only pipeline driven by DouyinLiveRecorder: the recorder wrote full video (`ts` → `mp4`), a post-record hook detached a job that waited for the file to stop growing, transcribed it with the BaoCut CLI or mlx-whisper, then polished and digested it. The redesign above replaces the recorder, the hook, video-by-default and BaoCut. Findings from that period that still hold:

- **No end-to-end tool.** No inspected open-source or commercial tool covered live capture → transcript → digest. Recorders (DouyinLiveRecorder, StreamCap, stream-rec, biliup) stop at files; transcript-to-notes tools (BiliNote, BaoCut) start from files.
- **Xiaohongshu share links are single-use.** DouyinLiveRecorder's source detects a live broadcast only from a live-room share link; a profile URL yields the name only. StreamCap documents the same. Unattended "follow this creator" monitoring is not implemented in any inspected open-source tool; one commercial Windows recorder claimed it, unverified. livedigest therefore disables a Xiaohongshu share link after its broadcast ends.
- **Post-record hooks are fragile.** DouyinLiveRecorder runs its hook only after a clean ffmpeg exit, and its MP4 conversion runs in background threads that the hook can race. Owning the ffmpeg process removed the problem.
- **Cloud options exist** (e.g. Alibaba Tingwu with real-time streaming ASR, paid per minute), but local transcription on Apple Silicon proved fast enough, so none is used.
- **Quality of the polish step.** On a 3-minute test recording, polish took about 25 s and cut 14–20% of the characters; corrections of domain terms varied from run to run, which is why the reading version lists doubtful corrections and the raw transcript is always kept.

## Field notes from live testing (2026-11-13)

- **Bilibili needs a Referer.** Without `Referer: https://live.bilibili.com/` some CDN hosts answer 403 at once and others after about 10 s, which looked like a flaky stream (one room reconnected 9 times in 12 minutes, 72% coverage). With the header, three rooms ran 12.7 minutes as one part each with no gaps.
- **One room, several hosts.** Bilibili returns several candidate URLs per room. All of them are kept in order; a host whose part dies early is avoided for ten minutes and the next one is tried within seconds. With the two known-bad hosts forced first, a good host was reached in under 20 s.
- **Lower quality is free where offered.** Douyin's low rendition of a game room was 2.3 Mbit/s at 720p instead of 18.8 Mbit/s at 1080p. Bilibili gives guests one quality whatever is asked.
- **Douyin** rooms captured without a cookie. **YouTube** stopped at its sign-in bot check in both streamget and yt-dlp without a cookie. **Xiaohongshu** was not tested in this version (share links are single-use).
- **mlx-audio chooses its decoder from the file extension** and cannot read `.ts`, so the Qwen3 engine decodes audio with ffmpeg itself.
