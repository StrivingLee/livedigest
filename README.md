# livedigest

**EN.** livedigest watches live rooms on Douyin, Bilibili, Xiaohongshu and YouTube and turns each broadcast into a timestamped transcript, a cleaned-up reading version and a short digest. It is audio-first: for each live room one ffmpeg process writes compact audio segments plus an occasional still, and the video is never written to disk unless you ask for it. Speech recognition runs locally on an Apple Silicon Mac while the stream is still live, so a transcript is ready minutes after the broadcast ends. Only the polish and digest steps call an external command-line LLM.

**ZH.** livedigest 监视抖音、B 站、小红书和 YouTube 的直播间，把每场直播变成带时间戳的转写稿、整理后的阅读版和一份简短摘要。它以音频为先：每个直播间由一个 ffmpeg 进程写出体积很小的音频分段和少量截图，除非你明确要求，视频永远不落盘。语音识别在 Apple Silicon Mac 上本地运行，直播进行中就开始转写，所以下播几分钟后就能拿到转写稿。只有整理（polish）和摘要两步会调用外部的命令行 LLM。

## How it works / 工作原理

```
rooms.toml ──► livedigest watch  (polls each room every 60 s via streamget)
                     │ room is live
                     ▼
     one ffmpeg per room ──► audio/p01_000.ts …   16 kHz mono AAC, 5-min MPEG-TS segments
                         ├─► frames/              one still per 10 s, keyframes only
                         └─► video/  (keep = "video" only)
                     │ each closed segment
                     ▼
     stills: slides → keep each distinct slide · camera → one still per 5 min
     one worker thread: Qwen3-ASR (mlx-audio) ──► transcripts/p01_000.json
                     │ room offline for 10 min = broadcast over
                     ▼
     merge ──► polish (LLM) ──► digest (LLM) ──► digests/<date>/<anchor>/<session>/
```

**EN.** A room that drops and comes back within 10 minutes continues the same session as a new *part* (`p02_…`). Each still window is classified with a 256-bit difference hash: a mostly unchanged picture with occasional jumps is *slides* (every distinct slide is kept and embedded in the transcript); a picture that is always moving is *camera* (one context still per 5 minutes). One model is in memory however many rooms are live. Design rationale and the options that were rejected are in [research.md](research.md).

**ZH.** 直播间断流后 10 分钟内恢复，会作为同一场的新 *part*（`p02_…`）继续。每个窗口的截图用 256 位差值哈希分类：画面大多不动、偶尔跳变的是 *slides*（保留每张不同的幻灯片并嵌入转写稿）；画面一直在动的是 *camera*（每 5 分钟留一张作为上下文）。无论同时有几个直播间在播，内存里只有一个模型。设计理由和被否决的方案见 [research.md](research.md)。

## Requirements / 环境要求

**EN.**
- macOS on Apple Silicon. All speech engines (Qwen3-ASR and MOSS-Transcribe-Diarize via mlx-audio, mlx-whisper) run on MLX.
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).
- ffmpeg and ffprobe on `PATH` (e.g. `brew install ffmpeg`).
- Optional: yt-dlp (YouTube fallback resolver); the `claude` CLI or any other command that reads a prompt on stdin and prints Markdown (polish and digest); OpenCLI (only for `add-video`).
- The first transcription downloads about 2.5 GB of model weights from Hugging Face (`mlx-community/Qwen3-ASR-1.7B-8bit`).

**ZH.**
- 搭载 Apple Silicon 的 macOS。所有语音引擎（mlx-audio 上的 Qwen3-ASR 与 MOSS-Transcribe-Diarize、mlx-whisper）都基于 MLX。
- Python ≥ 3.12 和 [uv](https://docs.astral.sh/uv/)。
- `PATH` 中有 ffmpeg 和 ffprobe（如 `brew install ffmpeg`）。
- 可选：yt-dlp（YouTube 备用解析）；`claude` CLI 或任何从 stdin 读提示词、输出 Markdown 的命令（用于整理和摘要）；OpenCLI（仅 `add-video` 需要）。
- 首次转写会从 Hugging Face 下载约 2.5 GB 模型权重（`mlx-community/Qwen3-ASR-1.7B-8bit`）。

## Quick start / 快速开始

```sh
uv sync
uv run livedigest add 'https://live.bilibili.com/123456'   # or paste a whole share message
# or: cp rooms.example.toml rooms.toml and edit it
uv run livedigest status      # paths, engine, rooms, ffmpeg / yt-dlp found?
uv run livedigest watch       # leave it running; Ctrl-C stops cleanly
```

**EN.** While a room is live, `digests/<date>/<anchor>/<session>/transcript.md` grows every 5 minutes. When the broadcast ends, the reading version and digest are written beside it, and `digests/index.md` lists every job. If you stop `watch` and do not plan to restart it, `livedigest finish` transcribes and finishes whatever is pending.

**ZH.** 直播期间，`digests/<日期>/<主播>/<场次>/transcript.md` 每 5 分钟增长一次。下播后，阅读版和摘要会写在同一目录，`digests/index.md` 列出所有任务。如果停掉 `watch` 后不打算再开，`livedigest finish` 会把待处理的内容转写完并收尾。

## Storage policy / 存储策略

Set per room with `keep` in `rooms.toml` or `livedigest add --keep`. / 在 `rooms.toml` 中用 `keep` 按直播间设置，或使用 `livedigest add --keep`。

| `keep` | Written to disk / 写入磁盘 | Typical size / 典型大小 |
|---|---|---|
| `auto` (default) | audio + stills, pruned to distinct slides or one still per 5 min | ~30 MB/h audio + 1–30 MB/h stills |
| `audio` | audio only | ~30 MB/h |
| `video` | as `auto`, plus a full stream copy in `video/` (30-min segments) | ~2 GB/h for 1080p |

**EN.** Audio is 16 kHz mono AAC at 48 kbit/s; stills are JPEG, at most 720 px high. A stream without a video track is captured as audio only. Disk guard: below `min_free_gb` (default 5 GB) free, no new capture starts and dropped rooms are not reconnected; below 1 GB, running captures are stopped.

**ZH.** 音频为 16 kHz 单声道 AAC、48 kbit/s；截图为 JPEG，高度不超过 720 像素。没有视频轨的流只录音频。磁盘保护：剩余空间低于 `min_free_gb`（默认 5 GB）时不再开始新录制、断流的直播间也不重连；低于 1 GB 时停止正在进行的录制。

## Output files / 输出文件

Capture, under `recordings_dir` (one folder per broadcast):

```
recordings/<platform>/<anchor>/<YYYY-MM-DD_HH-MM-SS>/
├── session.json          state, parts, still statistics
├── p01.ffmpeg.log
├── audio/p01_000.ts …    5-min audio segments (+ hidden .p01_segments.csv)
├── frames/p01_000120.jpg kept stills, named by seconds from session start
├── transcripts/p01_000.json   one per segment, times from session start
├── speakers.json         voices heard so far (engine = "moss" only)
└── video/p01_000.ts …    keep = "video" only
```

Results, under `output_dir`:

```
digests/
├── index.md, index.json  every job, newest first
├── failures.jsonl        stage = capture | transcribe | polish | digest
└── <date>/<anchor>/<session or file stem>/
    ├── meta.json, status.json
    ├── transcript.json   timed lines (source of truth)
    ├── transcript.srt
    ├── transcript.md     raw, timestamped; grows while live
    ├── polished.json
    ├── transcript.clean.md   reading version
    ├── digest.md
    └── frames/           slide stills, only when the session is mostly slides
```

**EN.** `transcript.md` and `.srt` stay verbatim for checking against the recording. `transcript.clean.md` removes filler, false starts and repetition, fixes homophones and glossary terms, and keeps every fact, number, name and hedge; each paragraph carries the timestamp of its first line, and doubtful corrections are listed at the end under 待核对. A paragraph whose rewrite looks like added or lost content falls back to the raw text with a warning. The digest has an overview, timestamped key points and a list of things worth checking.

**ZH.** `transcript.md` 和 `.srt` 保持逐字原样，用于对照录音核查。`transcript.clean.md` 删去口头禅、口误和重复，修正同音错字和术语表中的词，保留所有事实、数字、人名和表示把握程度的说法；每段带有其第一句的时间戳，拿不准的修改列在文末“待核对”。若某段改写看起来增加或丢失了内容，则退回原文并给出警告。摘要包含概要、带时间戳的要点和值得核对的列表。

## Commands / 命令

| Command | What it does / 作用 |
|---|---|
| `livedigest add TEXT [--keep auto\|audio\|video] [--name NAME]` | Add a room from a link or a whole pasted share message (b23.tv links are resolved; tracking parameters dropped). |
| `livedigest rooms` | List watched rooms. |
| `livedigest enable ROOM` / `disable ROOM` | Toggle a room by link or name; a live capture of a disabled room stops. |
| `livedigest watch` | Capture live rooms and transcribe as they go. One watcher at a time. |
| `livedigest finish` | Without capturing: transcribe pending segments, end sessions a stopped watcher left open, retry failed stages. |
| `livedigest process FILE… [--anchor A] [--redo transcribe\|polish\|digest]` | Transcribe, polish and digest local media files; `--redo` reruns a stage and every stage after it. |
| `livedigest add-video URL [--no-process]` | Download one Douyin video through OpenCLI, then process it. |
| `livedigest status` | Show paths, engine, rooms, free disk, cookies present and tools found. |

Global options: `--config PATH` (or `LIVEDIGEST_CONFIG`), `-v`.

## Configuration / 配置

**EN.** `livedigest.toml` holds everything except secrets; relative paths resolve against the file's own folder.
- `[capture]`: `segment_seconds` (300), `frame_interval` (10), `poll_seconds` (60), `merge_gap_seconds` (600, a room back within this is the same broadcast), `min_free_gb` (5.0), `quality = "low" | "best"` (default low: ask the platform for a smaller stream, since only speech and stills are kept; rooms with `keep = "video"` always get best), optional `proxy`.
- `[transcribe]`: `engine = "auto" | "qwen3" | "whisper" | "moss"` (auto picks Qwen3-ASR when mlx-audio is installed), `language`, `qwen3_model`, `whisper_model`, `moss_model`. `engine = "moss"` labels who is speaking; see [Speaker labels](#speaker-labels--说话人标签).
- `[polish]` and `[digest]`: `enabled`, `command` (default `["claude", "-p"]`, prompt on stdin, Markdown on stdout); `[digest] language` (default 简体中文). The prompts are written in Chinese.
- `[douyin] opencli`: path to OpenCLI for `add-video`; empty searches `$OPENCLI`, `PATH`, then the newest `~/.nvm` Node.
- `cookies.toml` (beside `livedigest.toml`, git-ignored): one cookie string per platform, e.g. `youtube = "..."`, passed to streamget for that platform.
- `glossary/_common.txt` and `glossary/<anchor>.txt`: one term per line, `#` starts a comment. Terms go to Qwen3-ASR as hotwords (to Whisper as part of its prompt) and to the polish step as preferred spellings.

**ZH.** 除密钥外的设置都在 `livedigest.toml`；相对路径以该文件所在目录为基准。
- `[capture]`：`segment_seconds`（300）、`frame_interval`（10）、`poll_seconds`（60）、`merge_gap_seconds`（600，在此时间内恢复的直播间算同一场）、`min_free_gb`（5.0）、`quality = "low" | "best"`（默认 low：只保留语音和截图，所以向平台请求较小的流；`keep = "video"` 的直播间总是用 best）、可选的 `proxy`。
- `[transcribe]`：`engine = "auto" | "qwen3" | "whisper" | "moss"`（auto 在装有 mlx-audio 时选 Qwen3-ASR）、`language`、`qwen3_model`、`whisper_model`、`moss_model`。`engine = "moss"` 会标出说话人，见[说话人标签](#speaker-labels--说话人标签)。
- `[polish]` 与 `[digest]`：`enabled`、`command`（默认 `["claude", "-p"]`，提示词从 stdin 读入，Markdown 从 stdout 输出）；`[digest] language`（默认 简体中文）。提示词是中文写的。
- `[douyin] opencli`：`add-video` 使用的 OpenCLI 路径；留空则依次查找 `$OPENCLI`、`PATH`、最新的 `~/.nvm` Node。
- `cookies.toml`（与 `livedigest.toml` 同目录，已被 git 忽略）：每个平台一行 cookie，如 `youtube = "..."`，解析该平台时传给 streamget。
- `glossary/_common.txt` 与 `glossary/<主播>.txt`：每行一个词，`#` 开始注释。这些词作为 hotwords 交给 Qwen3-ASR（Whisper 则写入其 prompt），并作为首选写法交给整理步骤。

## Speaker labels / 说话人标签

**EN.** With `engine = "moss"` the transcript says who is speaking. [MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize) returns text, a time for every sentence and a speaker number in one pass. It numbers speakers afresh in each five-minute segment, so livedigest takes a voice embedding (ECAPA-TDNN) of each speaker and matches it against the voices already heard in the broadcast, kept in `speakers.json` in the session folder. One person therefore keeps one label (`S01`, `S02`, ...) for the whole broadcast, including across reconnects and restarts. The longest speaker gets the lowest number. A voice heard for less than three seconds that matches nobody is labelled `S?`.

Labels appear in `transcript.md`, `transcript.srt` and `transcript.json`; the polished transcript starts a new paragraph when the speaker changes. The labels are anonymous: livedigest does not know names.

It costs speed: about 2.7× real time instead of about 10×, so one or two live rooms keep up and more will queue until broadcasts end. The first use downloads about 1.9 GB. It has been tried on one 15-minute Douyin auction recording (see the table below), not yet on a live capture or on a panel with many speakers. Glossary terms are passed to it as hotwords.

**ZH.** 设置 `engine = "moss"` 后，转写会标出谁在说话。[MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize) 一次推理同时给出文字、每句话的时间和说话人编号。它在每个 5 分钟分段里都从头编号，所以 livedigest 为每个说话人提取声纹向量（ECAPA-TDNN），与本场直播已出现过的声音比对；这些声音保存在会话目录的 `speakers.json` 中。这样同一个人在整场直播里（包括断线重连和重启之后）始终是同一个编号（`S01`、`S02`……）。说话最多的人编号最小。说话不足 3 秒且对不上任何已知声音的，标为 `S?`。

编号出现在 `transcript.md`、`transcript.srt` 和 `transcript.json` 中；整理版在换人说话时另起一段。编号是匿名的：livedigest 不知道姓名。

代价是速度：约 2.7 倍实时，而不是约 10 倍，所以一两个直播间能跟上，更多则要排队到直播结束后处理。首次使用会下载约 1.9 GB。目前只在一段 15 分钟的抖音拍卖录像上试过（见下表），尚未在实时录制或多人对谈上验证。术语表中的词会作为 hotwords 传给它。

## Platform status / 平台状态

| Platform / 平台 | Resolve room / 解析 | Live capture verified / 实测录制 | Notes / 说明 |
|---|---|---|---|
| Bilibili | yes | yes (three rooms concurrently, 12.7 min, no gaps) | `b23.tv` share links are resolved to the room URL. Works without a cookie. |
| Douyin | yes | yes (two rooms, 3 min each) | `v.douyin.com` share links and `live.douyin.com` rooms. H.265 rooms fall back to HLS. Works without a cookie. |
| Xiaohongshu | not tested | not tested | `xhslink.com` share links work once; the room is disabled automatically after its broadcast. |
| YouTube | blocked without a cookie | not tested | Without a cookie both streamget and yt-dlp stop at YouTube's "Sign in to confirm you're not a bot" check. Put a `youtube` cookie in `cookies.toml`; this path has not been tested with one. |

## Measured numbers / 实测数据

On a 16 GB Apple Silicon Mac / 在一台 16 GB 内存的 Apple Silicon Mac 上：

| Measure / 指标 | Result / 结果 |
|---|---|
| Disk, 1080p Douyin stream / 磁盘占用 | ~2 GB/h as video vs ~30 MB/h audio + 1–30 MB/h stills |
| ffmpeg capture CPU / 录制 CPU | under 1% of a core per stream (165× real time offline) |
| Qwen3-ASR 1.7B 8-bit speed / 转写速度 | ~10× real time: a 50-min recording in 292 s; a 60 s live segment in 5–7 s |
| MOSS-Transcribe-Diarize speed / 说话人引擎速度 | ~2.7× real time: 15 min of audio in 335 s; the host kept one label across three segments, two brief other voices were separated |
| Watcher memory, two rooms live / 内存 | ~2.7 GB footprint, flat (peak 3.9 GB) |
| Bilibili capture coverage, three rooms, 12.7 min / B 站录制覆盖率 | ~100%, one part per room, no gaps |
| Still-to-still hash distance, median / 截图哈希距离中位数 | slides lecture 11/256 · talking head 51 · product camera 85 |

## Limitations and known issues / 局限与已知问题

**EN.**
- Apple Silicon only for transcription; other platforms would need another engine.
- Speaker labels need `engine = "moss"`, which is slower and less tested than the default engine; the default has none.
- With Qwen3-ASR, timestamps inside each 20-second ASR chunk are interpolated by character count; chunk boundaries are exact.
- Transcription is one worker thread. At ~10× real time it keeps up with several rooms, but many concurrent rooms will queue.
- Stream URLs expire and CDNs drop connections. When ffmpeg exits, the room is re-resolved within seconds and capture continues as a new part; if the platform offers several stream URLs, a CDN host that just failed is avoided for a while. A short gap in the audio is still possible.
- Xiaohongshu share links are single-use: each broadcast needs a fresh link. There is no "follow this creator" mode.
- Platform pages and APIs change without notice; resolution depends on streamget (and yt-dlp for YouTube) keeping up.
- Polish and digest send transcript text to whatever `command` you configure; with `claude -p` it leaves your machine. Disable them or use a local command if that matters.
- Polish and digest prompts are tuned for Mandarin broadcasts.

**ZH.**
- 转写只支持 Apple Silicon；其他平台需要换引擎。
- 说话人标签需要 `engine = "moss"`，它比默认引擎慢，测试也更少；默认引擎不区分说话人。
- 使用 Qwen3-ASR 时，每个 20 秒 ASR 片段内部的时间戳按字数插值；片段边界是准确的。
- 转写只有一个工作线程。约 10 倍实时速度足以跟上几个直播间，但同时开播的直播间很多时会排队。
- 流地址会过期，CDN 也会断开连接。ffmpeg 退出后，几秒内重新解析该直播间并作为新 part 继续录制；若平台提供多个流地址，刚失败的 CDN 主机会被暂时避开。音频仍可能有短暂缺口。
- 小红书分享链接只能用一次：每场直播都要新链接，没有“关注主播”模式。
- 平台页面和接口会不经通知地变化；能否解析取决于 streamget（YouTube 还有 yt-dlp）是否及时跟进。
- 整理和摘要会把转写文本发给你配置的 `command`；使用 `claude -p` 时文本会离开本机。如介意，请关闭这两步或换成本地命令。
- 整理和摘要的提示词针对普通话直播调校。

## Responsible use / 合理使用

**EN.** Record only broadcasts you have the right to record, and follow each platform's terms of service and the creators' rights. livedigest is meant for personal study and note-taking: do not redistribute recordings, transcripts or digests of other people's broadcasts. Cookies you supply are used only to read streams your own account can already watch; livedigest does not and must not be used to bypass paywalls, DRM or other access controls.

**ZH.** 只录制你有权录制的直播，遵守各平台的服务条款，尊重创作者的权利。livedigest 用于个人学习和做笔记：请勿传播他人直播的录音、转写稿或摘要。你提供的 cookie 只用于读取你的账号本来就能观看的直播；livedigest 不会、也不得被用于绕过付费墙、DRM 或其他访问控制。

## Development / 开发

```sh
uv run pytest
uv run ruff check
```

## Acknowledgements / 致谢

**EN.** livedigest builds on streamget, mlx-audio, MLX, Qwen3-ASR, MOSS-Transcribe-Diarize, SpeechBrain ECAPA-TDNN, mlx-whisper / Whisper, FFmpeg, yt-dlp and OpenCLI, and learned much from DouyinLiveRecorder. See [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) for what each project contributed and its licence. Platform names, trademarks and content belong to their owners.

**ZH.** livedigest 构建在 streamget、mlx-audio、MLX、Qwen3-ASR、MOSS-Transcribe-Diarize、SpeechBrain ECAPA-TDNN、mlx-whisper / Whisper、FFmpeg、yt-dlp 和 OpenCLI 之上，并从 DouyinLiveRecorder 学到很多。各项目的贡献与许可证见 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)。平台名称、商标及内容归其所有者所有。

## Licence / 许可证

**EN.** MIT, see [LICENSE](LICENSE). Dependencies and models keep their own licences.

**ZH.** MIT 许可证，见 [LICENSE](LICENSE)。依赖和模型各自沿用其许可证。
