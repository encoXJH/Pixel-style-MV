# Pixel Narrative MV

一个可复用的 Trae Skill 与本地命令行生成器：**只需逐行歌词和对应歌曲音频，即可生成统一像素叙事 MV**。

工具会自动完成歌词对齐、音频节拍分析、像素场景与镜头规划、字幕和转场合成、FFmpeg 封装，并输出视频、联系表、关键帧及完整 QC 报告。默认使用本地程序化像素素材，不依赖外部文生图或文生视频服务，也不需要编写专业提示词。

## 功能特性

- **两输入生成**：歌词文本或 UTF-8 歌词文件，加对应的歌曲音频。
- **自动时间轴**：使用 stable-ts / faster-whisper 进行句级对齐，结合节拍、onset 与 RMS 分析规划镜头。
- **统一像素风格**：480×270 工作画布，4 倍最近邻放大至 1920×1080，30fps。
- **完整画面合成**：内置场景、角色、道具、滤镜、字幕、硬切、白闪、卷轴擦除与片尾收黑。
- **可追溯产物**：保存逐句对齐、音频特征、镜头时间轴、逐帧日志、联系表与关键帧。
- **三层自动 QC**：检查时间轴、渲染帧日志和最终 MP4；实际音频偏移超限时，在测量可信的情况下自动校正一次。
- **明确的草稿门禁**：低置信或估计时间轴只输出 `draft.mp4`，不会包装成正式成片。

## 下载与安装

### 作为 Trae 项目 Skill

1. 下载本仓库 ZIP 并解压，或克隆本仓库。
2. 确保目录名为 `pixel-narrative-mv`，且目录根部可以直接看到 `SKILL.md`。
3. 将整个目录放入目标项目的 `.trae/skills/` 下，最终结构为：

```text
你的项目/
└── .trae/
    └── skills/
        └── pixel-narrative-mv/
            ├── SKILL.md
            ├── assets/
            ├── references/
            └── scripts/
```

4. 在 Trae 中打开该项目，直接提出生成需求，例如：

> 使用 pixel-narrative-mv，以 lyrics.txt 为歌词、song.mp3 为音频生成像素叙事 MV。

也可以不通过 Trae，直接使用下面的命令行入口。

## 环境要求

- Windows PowerShell。
- Python 3.12，或可由脚本调用的 `uv`。
- `ffmpeg` 与 `ffprobe` 已加入 `PATH`，并支持 H.264/libx264 与 AAC。
- 可用的中文黑体字体，例如微软雅黑、黑体或 Noto Sans CJK。
- 首次安装依赖、首次下载对齐模型时需要网络；后续可复用本地 `.venv` 和 `.models` 缓存。

一键入口会在 Skill 根目录创建隔离的 Python 环境，并按照 `requirements.txt` 安装锁定依赖，不会使用仓库上传者的本地环境。

## 快速开始

### 1. 准备输入

歌词文件使用 UTF-8 编码，每行一句：

```text
第一句歌词
第二句歌词
第三句歌词
```

确保歌词和音频属于同一首歌。可以直接传歌词文本，也可以传 `.txt` 文件路径；音频可使用 FFmpeg 支持的常见格式。

### 2. 执行生成

在 Skill 根目录运行：

```powershell
scripts/run.ps1 -Lyrics ".\lyrics.txt" -Audio ".\song.mp3"
```

也可以直接传入歌词文本：

```powershell
scripts/run.ps1 -Lyrics "第一句歌词`n第二句歌词" -Audio ".\song.mp3"
```

路径包含空格或中文时，请使用引号。首次运行需要准备环境和模型，生成期间不要仅凭中间文件判断任务已经完成。

### 3. 查看结果

默认输出到**执行命令时当前目录**下的：

```text
outputs/<音频文件名>-pixel-mv/
```

需要指定位置时，可添加：

```powershell
scripts/run.ps1 -Lyrics ".\lyrics.txt" -Audio ".\song.mp3" -OutputDir ".\my-mv"
```

## 输出说明

| 文件或目录 | 用途 |
| --- | --- |
| `final.mp4` | 对齐状态为 `aligned` 且自动 QC 全部通过时的视频。 |
| `draft.mp4` | 估计/低置信时间轴或 QC 未通过时保留的视频草稿。 |
| `generation.json` | 最终输出路径、草稿状态、QC 结果和音频偏移摘要。 |
| `alignment.json` | 每句歌词的开始、结束、状态与置信度。 |
| `timeline.json` | 镜头、场景、道具、转场和字幕时间轴。 |
| `frames.jsonl` | 逐帧渲染记录。 |
| `qc.json` | 完整自动检查结果、错误与警告。 |
| `contact-sheet.jpg` | 全片抽样联系表，便于快速浏览。 |
| `keyframes/` | 每个镜头的开始、中间和结束关键帧。 |

`final.mp4` 与 `draft.mp4` 按本次结果二选一。建议同时查看视频、`generation.json`、`qc.json` 和联系表，不要只根据进程退出码判断艺术质量。

## 人工修正时间轴

如果自动对齐降级为草稿，可以编辑首次生成的 `alignment.json`，修正每句的 `start` 和 `end` 后重跑：

```powershell
scripts/run.ps1 -Lyrics ".\lyrics.txt" -Audio ".\song.mp3" -Alignment ".\alignment-fixed.json" -OutputDir ".\my-mv-fixed"
```

歌词、行数和音频必须与首次输入一致。每句时间必须有限、严格单调且不得重叠；`status` 只接受 `aligned` 或 `estimated`，`confidence` 必须为 `[0,1]` 内的有限数值。任一句为 `estimated` 时，整体仍按草稿处理。

高级参数、输出字段及故障处理见[使用、参数与输出](references/usage.md)。

## 是否需要专业提示词

**当前版本不需要。**它不是通过提示词调用外部生成模型，而是根据歌词关键词、内置素材和确定性规则完成像素画面与镜头规划。

因此，增加提示词不会直接解锁任意新角色或新场景。若需要高度接近某条参考视频，还需要定制分镜、角色与场景素材、动作设计以及人工艺术调整；当前版本不承诺复刻任意参考作品或原作者工程。

## QC 与真实性边界

- 50ms 自动门禁覆盖内部时间轴量化误差，以及最终封装音频相对源音频的实测偏移。
- 没有人工参考时间轴时，不能证明真实人声误差必然不超过 50ms；`human_vocal_sync` 始终保持 `unverified`。
- `passed=true` 只表示自动检查通过，不等于估计时间轴变成正式成片，也不等于人工艺术验收通过。
- 转场检查覆盖帧区间、异常黑帧、冻结和帧差等指标，不证明主观转场自然度。
- 默认不复制任何原作者素材，也不保证与参考作品具有相同艺术质量。

完整指标解释见[验证与真实性边界](references/validation.md)，视觉规范见[风格规范](references/style-spec.md)。

## 开发与测试

首次运行生成器、准备好 `.venv` 后，可执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

包含真实渲染和 FFmpeg QC 的完整测试：

```powershell
$env:PIXEL_MV_E2E = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 目录结构

```text
pixel-narrative-mv/
├── SKILL.md                 # Skill 路由与执行约束
├── README.md                # 下载、安装与使用说明
├── requirements.txt         # 锁定的 Python 依赖
├── assets/style.json        # 统一视觉与时间轴配置
├── scripts/                 # 对齐、渲染、QC、封装与一键入口
├── references/              # 参数、风格和验证文档
└── tests/                   # 单元与端到端测试
```

维护者：encoXJH
