# CutMaster

<p align="center">
  <a href="README.md"><kbd>中文</kbd></a>
  &nbsp;|&nbsp;
  <a href="README_EN.md"><kbd>English</kbd></a>
</p>

<p align="center">
  <img src="assets/cutmaster_pipeline.png" alt="CutMaster 方法总览" width="100%">
</p>

<p align="center"><em>CutMaster：面向长视频混剪的节拍感知流水线</em></p>

CutMaster 是一个纯后端长视频混剪流水线。它接收一部长视频、一条背景音乐和一条自然语言指令，生成时间轴精确到帧的音乐混剪。项目从 Mashup-Benchmark 的 NarratoAI adapter 所使用的生产流程中提取并扩展而来，现已成为独立的 Python 项目。

CutMaster 当前支持 LLM 辅助台词重组、结构化音乐分析、抽象剪辑 Slot 规划、基于 ASR 的多候选检索、带时序依赖的 Beam Search、版本化脚本补丁、结合视觉切点的原片窗口优化，以及确定性的 FFmpeg 渲染。最终视频会静音原片音频，只保留指定的背景音乐。

## 流水线

```text
原始视频 + 背景音乐 + 用户指令
  -> 校验输入；除非指定 --overwrite，否则保护已有输出
  -> 复用用户提供/已有的 SRT，或使用 DashScope Fun-ASR 转写
  -> 并行重组完整台词，同时保留原始字幕锚点
  -> 使用 librosa 生成节拍、重音、能量曲线和音乐段落的结构化画像
  -> LLM 根据指令与音乐画像规划抽象剪辑 Slot，不生成原片时间戳
  -> 全局调整 Slot 时长，将所有输出边界对齐到音乐重音
  -> LLM 基于 ASR 台词为每个 Slot 检索若干原片候选
  -> 从真实视频计算候选片段运动强度
  -> 同时计算逐 Slot 独立最优路径和带时序依赖的 Beam Search 路径
  -> LLM 在既有候选池内复核并以补丁方式修改脚本
  -> 并行检测每个候选窗口内的原片切点
       - 在自适应场景检测前过滤近重复帧
       - 所有保留帧继续使用原片时间戳
       - 每个原片窗口最多向后搜索 2 秒
       - 最小化所有内部切点到最近音乐节拍的最大距离
       - 内部切点与片段边界优先保持至少 1 秒距离
  -> 按精确输出帧数渲染每个片段
  -> 拼接标准化的无声视频片段
  -> 循环并淡出背景音乐，保持原片音频静音
  -> output.mp4 + 结构化中间产物
```

### 台词重组

Fun-ASR 最初会将转写结果切分为较短的字幕条目。CutMaster 将同一说话人的相邻字幕组织为候选段落，再调用文本模型判断哪些完整段落应当合并。请求并发数由 `llm.max_concurrency` 控制。

`dialogues.json` 同时记录重组后的完整句子时间范围以及对应的每一个原始字幕锚点。`dialogue_merged.srt` 是传给剪辑脚本生成阶段的句子级字幕。台词重组是一个约束明确的边界选择任务，因此会关闭模型 thinking。

### 音乐画像与抽象规划

CutMaster 使用 `librosa` 计算 RMS、onset strength、spectral centroid 和节拍，并生成统一的 `music_profile.json`。画像包括能量时间序列、普通节拍、强重音、段落边界、段落角色，以及根据段落能量建议的片段时长范围。背景音乐短于目标视频时，节拍和重音会按照最终循环方式同步扩展。

初始规划会开启模型 thinking。默认 Slot 数量为：

```text
ceil(target_duration / target_shot_length)
```

模型只描述每个 Slot 应承载的内容、叙事作用、目标情绪强度、目标运动强度、与前一片段的衔接关系和期望时长，不允许给出原片时间戳。随后通过全局动态规划同时修正全部边界，使剪辑点落在音乐重音上，并保持单调、非零的片段时长。

### 候选检索、路径选择与补丁

候选检索模型根据 Slot 描述和 `dialogue_merged.srt`，为每个 Slot 返回若干带 ASR 证据的原片区间。每个候选必须满足：

- 与输入字幕时间轴有交集；
- 长度足以容纳对应 Slot；
- 包含非空的内容描述与台词证据；
- 使用合法的原片时间戳。

CutMaster 对候选计算语义相关性、情绪匹配、画面运动匹配、显著性和时长可行性等单片段分数。它同时保留：

- 每个 Slot 单独取最高分候选得到的独立最优路径；
- 将相邻片段连续性、原片时间关系和音乐能量变化纳入路径分数的 Beam Search 全局路径。

选择后，复核 LLM 只能使用候选池中已有的 `candidate_id` 进行 `keep/replace` 补丁，不能直接创造新时间戳。`planning_history.json` 会记录请求上下文、每次模型调用、响应状态、结构化结果、脚本版本和补丁历史。CutMaster 对完整的 API 请求、JSON 解析和语义校验事务进行指数退避重试，不会接受残缺结果。

### 视觉切点优化

对于每个选中的原片区间，CutMaster 会在该区间及其向后两秒的搜索范围内检测内部视觉切点。当前使用 PySceneDetect 的 `AdaptiveDetector`，默认参数为：

- 自适应阈值：`2.0`；
- 最小内容变化值：`15.0`；
- 最短场景长度：`0.25s`；
- 近重复帧阈值：灰度平均绝对差 `< 1.0`。

近重复帧过滤对于由较低帧率素材生成的 50/60 fps 视频十分重要。如果不做过滤，交替出现的“重复帧/新帧”会让自适应检测器将普通运动误判为大量切点。过滤只影响参与检测的帧，所有保留帧仍携带原视频中的真实时间码。

候选起点会在原片帧网格上从初始位置搜索至 `+2s`。Minimax 目标首先最小化任意内部输出切点到最近背景音乐节拍的最大距离，然后依次偏好更小的向后位移和更少的内部切点。

内部切点与片段边界的期望安全距离为 `1.0s`。如果没有可行候选窗口，CutMaster 会依次尝试 `0.75s`、`0.5s`、`0.25s`，最后尝试 `0.0s`。任何约束放宽都会记录在 `script_adapted.json` 中并输出 warning。`0.0s` 仅是保证任务完成的最后手段，不是正常优化目标。

### 帧精确渲染

每个适配后的片段都包含 `output_frame_range`。FFmpeg 会按照配置的分辨率和 FPS 精确渲染对应数量的帧，并移除原片音频。所有片段拼接后不会改变已规划的时间轴；随后背景音乐会循环、裁剪到混剪的精确时长、执行淡出并编码为 AAC。

当 `render.encoder = "auto"` 时，编码器按以下顺序选择：

1. macOS 上可用的 `h264_videotoolbox`；
2. 可用的 `h264_nvenc`；
3. 其他情况下使用 `libx264`。

## 环境要求

- Python `3.12`（`>=3.12,<3.13`）
- `uv`
- `PATH` 中可用的 `ffmpeg` 和 `ffprobe`
- 用于默认 LLM 和 Fun-ASR 配置的 DashScope API Key

项目暂不支持 Python 3.13，因为当前使用的 librosa/Numba 节拍跟踪路径在该环境中不稳定。

## 安装

```bash
uv sync
cp config.example.toml config.toml
```

`config.toml` 已被 Git 忽略。配置加载器支持直接通过 `api_key` 保存密钥，也支持通过 `api_key_env` 指定环境变量名称。

若采用环境变量，请将 `[llm]` 和 `[asr]` 中的 `api_key` 均替换为：

```toml
api_key_env = "DASHSCOPE_API_KEY"
```

运行 CutMaster 前导出密钥：

```bash
export DASHSCOPE_API_KEY="..."
```

## 配置

### `[llm]`

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `model` | OpenAI-compatible 文本模型 | `qwen3.7-plus` |
| `base_url` | OpenAI-compatible API Base URL | DashScope compatible-mode URL |
| `api_key` / `api_key_env` | 直接密钥或环境变量名称 | 占位值 |
| `temperature` | 采样温度 | `0.1` |
| `max_tokens` | 最大输出 token 数 | `4000` |
| `timeout_sec` | 单次模型请求超时 | `180` |
| `max_retries` | 首次请求失败后的重试次数 | `3` |
| `max_concurrency` | 台词重组批次并发数 | `4` |

OpenAI SDK 自身的重试已关闭，由 CutMaster 负责完整的“请求/解析/校验”重试周期。因此 `max_retries = 3` 表示最多执行四次完整请求，失败后的等待时间依次为 `1s`、`2s`、`4s`。

### `[planning]`

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `candidates_per_slot` | 每个 Slot 请求的原片候选数 | `4` |
| `retrieval_batch_size` | 单次候选检索请求包含的 Slot 数 | `5` |
| `beam_width` | Beam Search 保留的路径数量 | `8` |
| `review_rounds` | 候选内脚本补丁复核轮数 | `1` |
| `motion_sample_fps` | 候选运动强度的采样帧率 | `2.0` |
| `motion_workers` | 候选运动特征解码 worker 数 | `4` |

### `[asr]`

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `backend` | ASR 后端；当前仅支持 `bailian` | `bailian` |
| `api_key` / `api_key_env` | 直接密钥或环境变量名称 | 占位值 |
| `reuse` | 复用非空 `source.srt` 和已提取的 ASR 音频 | `true` |
| `timeout_sec` | 异步 ASR 总超时 | `1800` |
| `poll_interval_sec` | ASR 任务轮询间隔 | `2` |
| `max_chars` | 初始字幕条目的期望最大字符数 | `20` |
| `max_subtitle_duration_sec` | 初始字幕条目的期望最大时长 | `3.5` |

Fun-ASR 会开启说话人分离。上传前，FFmpeg 会提取 16 kHz 单声道的 `source_audio.m4a`。

### `[render]`

| 配置项 | 含义 | 默认值 |
| --- | --- | --- |
| `width`, `height` | 输出画布 | `1920×1080` |
| `fps` | 输出帧率和时间轴网格 | `30` |
| `encoder` | FFmpeg 视频编码器或 `auto` | `auto` |
| `threads` | 原片切点优化 worker 数和 libx264 线程数 | `8` |
| `bgm_volume` | 最终背景音乐音量倍率 | `0.3` |
| `original_volume` | 原片音频音量；帧精确模式要求为 `0` | `0.0` |
| `audio_sample_rate` | 最终 AAC 采样率 | `48000` |

`threads` 是单个进程的配置，而不是整台机器的全局并发上限。并行启动多个 CutMaster 进程会成倍增加视频解码器数量和内存占用。例如，同时运行 5 个外层进程且 `threads = 8` 时，最多可能产生 40 个并发原片切点 worker。

## 使用方法

```bash
uv run cutmaster run \
  --video /path/to/source.mp4 \
  --audio /path/to/bgm.mp3 \
  --prompt "剪出所有决定比赛走向的进球" \
  --output-dir outputs/demo \
  --target-duration 60 \
  --target-shot-length 4 \
  --prompt-type event
```

`run` 命令支持以下参数：

| 参数 | 是否必需 | 说明 |
| --- | --- | --- |
| `--video PATH` | 是 | 长视频原片 |
| `--audio PATH` | 是 | 背景音乐 |
| `--prompt TEXT` | 是 | 混剪指令 |
| `--output-dir PATH` | 是 | 中间产物和输出目录 |
| `--config PATH` | 否 | TOML 配置；默认为 `config.toml` |
| `--subtitle PATH` | 否 | 已有 SRT；提供后跳过 Fun-ASR |
| `--target-duration SEC` | 否 | 目标输出时长；默认 `60` |
| `--target-shot-length SEC` | 否 | 期望片段时长；默认 `4` |
| `--prompt-type TYPE` | 否 | 提供给脚本生成阶段的元数据；默认 `event` |
| `--video-title TEXT` | 否 | 提供给模型的人类可读原片标题 |
| `--custom-clips N` | 否 | 覆盖自动计算的片段数量 |
| `--max-clip-duration SEC` | 否 | 时长适配阶段使用的片段硬上限 |
| `--overwrite` | 否 | 覆盖已有运行结果 |

如果 `output.mp4` 已存在，运行会直接停止，除非指定 `--overwrite`。使用 `--overwrite` 时，如果 `asr.reuse` 已开启，已有 ASR 产物仍可能被复用；后续台词处理、脚本、切点优化和渲染产物会重新生成。

## 输出文件

每个输出目录包含：

| 路径 | 内容 |
| --- | --- |
| `source.srt` | 用户提供、缓存复用或 Fun-ASR 生成的原始字幕 |
| `source_audio.m4a` | 16 kHz 单声道 ASR 输入；仅在需要转写时创建 |
| `dialogues.json` | 重组后的句子、合并操作和原始字幕锚点 |
| `dialogue_merged.srt` | 用于候选检索的句子级字幕 |
| `music_profile.json` | 音乐能量、节拍、重音、段落和建议时长 |
| `edit_plan.json` | 重音对齐后的抽象剪辑 Slot，不含原片时间戳 |
| `candidate_pool.json` | 每个 Slot 的 ASR 候选、模型分数和本地运动特征 |
| `selection_diagnostics.json` | 独立最优路径、Beam Search 路径及路径分数 |
| `planning_history.json` | 主动维护的规划上下文、模型调用和脚本版本/补丁历史 |
| `script_raw.json` | 最终选中的候选路径及 Slot/候选 ID |
| `script_adapted.json` | 输出帧范围、节拍对齐、优化后的原片范围和切点诊断信息 |
| `clips/clip_XXXX.mp4` | 标准化的无声中间视频片段 |
| `montage.mp4` | 混入背景音乐前拼接得到的无声视频 |
| `output.mp4` | 带循环/淡出背景音乐、原声静音的最终视频 |
| `result.json` | 最终路径、时长、片段数、总耗时和各阶段耗时 |
| `cutmaster.log` | INFO/DEBUG 后端运行日志 |

`script_adapted.json` 中的每个片段还会记录：

- `output_timestamp` 和 `output_frame_range`；
- 优化后的原片 `timestamp`；
- 检测到的原片/输出切点时间戳；
- 优化前后切点到节拍的最大距离；
- 原片位移、边界安全距离 fallback 等级和实际生效距离。

## 包结构

- `asr.py`：音频提取、DashScope 上传、异步 Fun-ASR 轮询、说话人字幕转换和 ASR 复用。
- `dialogue.py`：候选段落构建、并行 LLM 边界选择、完整句子重组和字幕锚点保留。
- `llm.py`：OpenAI-compatible 客户端和完整 JSON 事务重试。
- `beats.py`：librosa onset envelope 和动态规划节拍跟踪。
- `music.py`：音乐能量、节拍、重音、段落结构和动态片段时长分析。
- `planning_context.py`：规划上下文、调用历史、结构化产物和脚本版本持久化。
- `planning.py`：抽象 Slot 规划、ASR 候选检索、运动特征、Beam Search 和候选内补丁。
- `script.py`：选定候选的时长适配、输出时间轴校验和帧网格量化。
- `cuts.py`：感知重复帧的 PySceneDetect 分析，以及并行、仅向后、帧级 minimax 原片窗口优化。
- `renderer.py`：编码器选择、帧精确片段渲染、视频拼接和最终 AAC 背景音乐混合。
- `pipeline.py`：端到端编排、输入校验、阶段计时和结果输出。
- `cli.py`：命令行入口。

## 当前范围与限制

- 候选检索依赖字幕。缺少有效邻近台词的纯视觉重要事件，目前仍更难进入候选池。
- 当前运动特征使用低分辨率帧差近似画面活动程度，还不是稠密光流或语义动作识别。
- 每次运行只接受一部原片和一条背景音乐。
- 原片音频会被主动静音；帧精确渲染器会拒绝 `original_volume > 0` 的配置。
- 项目不包含 UI、Web 任务队列、TTS、旁白字幕、素材搜索或 benchmark 专用运行记录。
- CutMaster 运行时不需要导入或安装 NarratoAI。

## 验证

```bash
uv run pytest
uv run python -m cutmaster --help
uv run python -m cutmaster run --help
```

## 开源归属

初始工作流源自采用 MIT 许可证的 NarratoAI 项目。详情参见 `THIRD_PARTY_NOTICES.md` 和 `LICENSE`。
