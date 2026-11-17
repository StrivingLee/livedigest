# Acknowledgements / 致谢

**EN.** livedigest is a small amount of glue around other people's work. This page records what each project contributed and how livedigest uses it. Licences were checked against installed package metadata or the project's repository in November 2026; each project's own licence file is authoritative.

**ZH.** livedigest 只是把别人的成果串起来的一层薄薄的代码。本页记录每个项目贡献了什么、livedigest 如何使用它。许可证核对自已安装包的元数据或项目仓库（2026 年 11 月）；以各项目自己的许可证文件为准。

## Runtime dependencies / 运行时依赖

Installed by `uv sync` (Python packages) or by you (command-line tools). livedigest does not vendor or redistribute any of them.

| Project | Licence | What livedigest uses it for |
|---|---|---|
| [streamget](https://github.com/ihmily/streamget) (ihmily) | MIT | Turns a room link into a live stream URL for all four platforms (Douyin, Bilibili, Xiaohongshu, YouTube); see `src/livedigest/resolve.py`. |
| [mlx-audio](https://github.com/Blaizzy/mlx-audio) (Prince Canuma / Blaizzy) | MIT | Runs the default speech-recognition model, Qwen3-ASR, and the optional speaker-aware models on Apple Silicon. |
| [MLX](https://github.com/ml-explore/mlx) (Apple) | MIT | The array framework under mlx-audio and mlx-whisper. |
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) (Alibaba Qwen team) | Apache-2.0 | The default speech-recognition model. Weights are the 8-bit MLX conversion [`mlx-community/Qwen3-ASR-1.7B-8bit`](https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-8bit) (model card: Apache-2.0), downloaded from Hugging Face on first use. |
| [MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize) (OpenMOSS team) | Apache-2.0 | Optional engine (`engine = "moss"`): one model that returns text, sentence times and a speaker label per line. Weights are [`OpenMOSS-Team/MOSS-Transcribe-Diarize`](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize), run through mlx-audio. |
| [SpeechBrain](https://github.com/speechbrain/speechbrain) ECAPA-TDNN (VoxCeleb) | Apache-2.0 | Voice embeddings that keep one speaker label per person across audio segments (only with `engine = "moss"`). Weights are the MLX conversion [`aufklarer/SpeechBrain-ECAPA-VoxCeleb-20M-MLX`](https://huggingface.co/aufklarer/SpeechBrain-ECAPA-VoxCeleb-20M-MLX) (model card: Apache-2.0), loaded by mlx-audio. |
| [mlx-whisper](https://github.com/ml-explore/mlx-examples) (MLX examples, Apple) | MIT | Fallback engine (`engine = "whisper"`, or `auto` without mlx-audio). |
| [Whisper](https://github.com/openai/whisper) (OpenAI) | MIT (code and weights) | The fallback model, as the MLX conversion `mlx-community/whisper-large-v3-turbo`. |
| [FFmpeg](https://ffmpeg.org/) | LGPL-2.1-or-later, with optional GPL parts ([legal](https://ffmpeg.org/legal.html)) | Called as an external program: reads the live stream, writes the audio segments and the keyframe stills, optionally copies the video; decodes audio for the ASR engine; hashes stills. |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | Optional. Fallback resolver for YouTube rooms when streamget fails. |

The polish and digest steps call a command-line LLM, `claude -p` (Claude Code) by default. Any command that reads a prompt on stdin and prints Markdown can replace it; livedigest does not depend on a particular provider.

## Earlier recorder / 早期录制工具

| Project | Licence | Contribution |
|---|---|---|
| [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) (ihmily) | MIT | The recorder that earlier versions of this project drove. It is no longer a dependency, but its source taught the per-platform details that `capture.py` and `resolve.py` now encode: ffmpeg input options (timeouts, probe sizes, a mobile User-Agent, no `-re` so CDNs do not stall a throttled reader), falling back from Douyin's H.265 FLV to HLS, and that Xiaohongshu share links work only once. |

## Design references (no code used) / 设计参考（未使用其代码）

| Project | Licence | What it informed |
|---|---|---|
| [stream-translator-gpt](https://github.com/ionic-bond/stream-translator-gpt) (ionic-bond) | MIT | The capture → chunk → ASR shape for live streams. |
| [WhisperLive](https://github.com/collabora/WhisperLive) (Collabora) | MIT | Same: how live ASR systems cut audio before recognition. |
| [whisper_streaming](https://github.com/ufal/whisper_streaming) (ÚFAL) | MIT | Same; reading it weighed true streaming ASR against transcribing closed segments (see [research.md](research.md)). |
| [bililive-go](https://github.com/bililive-go/bililive-go) | GPL-3.0 | The idea of an audio-only setting per room (`keep` here). |
| [BiliNote](https://github.com/JefferyHcool/BiliNote) (JefferyHcool) | MIT | Transcript → structured notes with an LLM. |
| [BaoCut](https://github.com/JimLiu/baocut) (JimLiu) | BaoCut Community License 1.0 (source-available) | Its CLI was the transcription engine of an earlier version; it showed what Qwen3-ASR with hotwords does for Mandarin. |
| [StreamCap](https://github.com/ihmily/StreamCap) (ihmily) | Apache-2.0 | Evaluated as a manual recorder in the first round of research. |

## Platforms / 平台

**EN.** Douyin (抖音), Bilibili (哔哩哔哩), Xiaohongshu (小红书) and YouTube are named only to say which sites livedigest can read. Their names, trademarks and content belong to their respective owners. livedigest is not affiliated with or endorsed by any of them.

**ZH.** 抖音、哔哩哔哩、小红书和 YouTube 仅用于说明 livedigest 能读取哪些网站。其名称、商标及内容归各自所有者所有。livedigest 与上述平台均无关联，也未获其认可。
