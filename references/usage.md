# 使用、参数与输出

本文对应 `scripts/run.ps1` 和 `scripts/mv.py` 的实际入口。常规生成只需要歌词与歌曲音频；其余参数均为可选高级配置。

## 1. 环境与首次运行

- 在 Windows PowerShell 中使用一键入口。
- 将 `ffmpeg` 与 `ffprobe` 加入 `PATH`；FFmpeg 需能使用 H.264/libx264 与 AAC 编码。
- 首次运行需要可用的 Python 3.12，或可由脚本调用的 `uv`。脚本在 Skill 根目录创建/复用 `.venv`，按 `requirements.txt` 安装锁定依赖。
- 首次安装依赖、首次下载对齐模型时可能需要网络。模型缓存位于 Skill 根目录的 `.models`；后续复用本地缓存。
- 需要可用的中文黑体字体。自动查找微软雅黑、黑体、Noto Sans CJK 等字体，也可用自定义样式中的 `subtitle.font_path` 指定。

## 2. 正常两输入入口

在 Skill 根目录执行：

```powershell
scripts/run.ps1 -Lyrics <路径或文本> -Audio <音频>
```

将占位内容替换为实际值，并给含空格的路径或文本加引号。例如 `-Lyrics "D:\歌词\song.txt" -Audio "D:\音频\song.mp3"`。直接传入多行歌词时，应把完整多行文本作为一个 `-Lyrics` 参数传入。

Python 等价入口：

```powershell
python scripts/mv.py --lyrics <路径或文本> --audio <音频>
```

这里的 `python` 必须是已安装锁定依赖的 Python 3.12。若使用一键脚本创建的隔离环境，等价写法为：

```powershell
.\.venv\Scripts\python.exe scripts/mv.py --lyrics <路径或文本> --audio <音频>
```

`mv.py` 是生成器入口，不负责像 `run.ps1` 那样自动创建环境或安装依赖。

### 输入规则

- `-Lyrics` / `--lyrics`：UTF-8 歌词文件路径，或直接歌词文本。空行会被忽略，常见 LRC 时间标签会被清除；清理后至少保留一行非空歌词。
- `-Audio` / `--audio`：对应歌曲的可解码音频文件，必须具有有效音轨和时长。最终封装使用该源音频，而不是另配音乐。
- 无换行的 `-Lyrics` 字符串若恰好是现有文件路径，会按文件读取；否则按直接文本处理。
- 强混响、多声部、说唱、重叠演唱或缺乏清晰人声时，自动对齐可能降级为估计时间轴。

默认输出到**执行命令时的当前目录**下的 `outputs/<音频文件名去扩展名>-pixel-mv/`。需要固定位置时使用 `-OutputDir`；从其他工作目录调用脚本时，不要误以为默认输出一定位于 Skill 根目录。

## 独立美术预览（不生成 MV）

修改 Skill 美术或只需检查构图时，使用独立预览入口。先按正常入口准备 `.venv`，再在 Skill 根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts/preview.py --output-dir ".\outputs\art-preview"
```

可添加 `--lyrics <路径或文本>` 和 `--style <JSON>`；省略歌词时使用内置虚构样例。`--output-dir` 必填，应使用独立目录，不要指向已有歌曲作品输出目录。

预览输出五类美术证据：

- 至多 12 张 1920×1080 代表帧和三列 `contact-sheet.jpg`，检查角色统一、镜头尺度、场景细节、环境变化、信息密度和紧凑字幕框。
- `motion/<shot_id>/` 中每个代表动作的 6 个本地帧，以及 `motion-contact-sheet.jpg`，检查翻牌、行走、骑行、阅读、弹奏、相拥、轨迹画心、列车、爱心、烟花和整数运镜。
- `transitions/<shot_id>` 中每个实际转场的过程帧，以及 `transition-contact-sheet.jpg`，检查匹配裁切、有序溶解、胶片擦除、照片显现、白闪和卷轴擦除。
- `weather/` 与 `weather-contact-sheet.jpg`：晴天、多云、雨天、雪天、晚霞、夜晚各 3 帧，检查独立配色、环境层次和粒子动态。
- `weather/transitions/` 与 `weather-transition-contact-sheet.jpg`：晴→多云、多云→雨、雨→雪、雪→晚霞、晚霞→夜晚的过程帧，检查有序像素替换和粒子密度变化。
- `style-qc.json` 记录叙事字段、动作/特效/运镜词表、密度、转场配置、尾奏构图多样性、间奏歌词绑定和片头片尾回环的结构校验；`preview.json` 索引全部预览并列出人工复核项目。

预览使用等分估计时间轴，不读取音频、不生成视频、不做同步或完整技术 QC；`preview.json` 明确标记 `preview_only=true`、`timing_status=estimated_equal_duration`、`audio_sync=not_evaluated` 和 `human_vocal_sync=unverified`。结构校验不证明主观相似度，美术预览也不能替代正式生成后的 QC、人工看图与试听。

## 3. 可选参数

| PowerShell | Python | 用途 |
| --- | --- | --- |
| `-OutputDir <目录>` | `--output-dir <目录>` | 指定输出目录；省略时使用上述默认目录。 |
| `-Alignment <JSON>` | `--alignment <JSON>` | 使用人工修正的句级时间轴重跑。 |
| `-Style <JSON>` | `--style <JSON>` | 替换默认 `assets/style.json`，仅供高级调整。 |

自定义样式不会改变当前固定渲染契约：30fps、480×270 工作画布、4 倍放大和 1920×1080 输出会被时间轴校验强制检查。不要通过放宽 QC 阈值把不合格结果包装成通过。

## 4. 输出文件

正常完成生成与封装后，输出目录包含以下核心产物；中途 fatal 失败时不保证已生成全部文件。

| 文件或目录 | 内容与用途 |
| --- | --- |
| `final.mp4` | `alignment_status=aligned` 且 QC 全部通过时的正式成片。仍不代表真人声同步已经人工验证。 |
| `draft.mp4` | 低置信/估计对齐时的完整视频草稿，或 QC 未通过时保留的草稿；与 `final.mp4` 按本次结果二选一。 |
| `alignment.json` | 歌曲时长、语言、后端、状态、置信度、警告及逐句 `id/text/start/end/status/confidence`。 |
| `audio-features.json` | `title`、`tempo`、`beats`、`onsets`、`rms_times`、`rms`、`section_times`、分析后端与警告。 |
| `timeline.json` | 帧率、总帧数、种子、尺寸、统一 `lyric_cues` 与 `sync` 声明，以及逐镜头 `kind`、场景、角色、表情、构图变体、道具、滤镜、叙事阶段、章节、动作、调度、整数运镜、特效、声音 cue、密度、转场、天气和共享字幕帧号。 |
| `frames.jsonl` | 每帧一行的渲染记录，包括帧号、PTS、镜头、场景、`visual_line_id/lyric_cue_id/lyric_line_id/subtitle_id`、天气状态与过渡阶段、精确帧差、转场、亮度、色板大小、字幕溢出和对比度等。 |
| `style-qc.json` | 风格档案、导演字段、动作/特效/运镜词表、密度、转场配置、尾奏多样性、间奏歌词绑定和片头片尾回环的结构校验及覆盖清单；不是主观相似度评分。 |
| `sound-design.json` | 原创环境/动作 cue 的类型、镜头、时刻、时长、增益、混音参数和参考音频不可用声明。 |
| `sound-cues.wav` | 48kHz、立体声、16-bit PCM 的原创程序化 cue 轨；不是参考作品音轨。 |
| `qc.json` | 时间轴、渲染日志和最终 MP4 的汇总检查、七项门禁、歌词/天气逐帧匹配率、误差、音频偏移、错误与警告。 |
| `generation.json` | 最终输出路径、QC/联系表路径、风格档案与结构校验、声音设计与 cue 数量、`passed`、`draft`、对齐状态、音频偏移、是否校正和 `human_vocal_sync`；`reference_mix_recreated=false`。 |
| `contact-sheet.jpg` | 默认每 5 秒抽样的全片联系表，带时间戳，用于快速浏览。 |
| `keyframes/` | 每个镜头 start/mid/end 的 PNG 关键帧；为放大并合成字幕后的高分辨率画面。 |

`work/` 保存视频中间文件和初次 QC 等过程产物，不替代上述交付物。完整歌曲通过流式管道渲染，不会默认保存全部视频帧为 PNG。

## 5. 人工修正 alignment 后重跑

这是可选的高级修正流程，不是两输入正常流程的额外必填步骤。

1. 保留同一份歌词和音频，复制首次生成的 `alignment.json` 供编辑。
2. 按真实演唱修正 `lines` 中每句的 `start`、`end`，单位为秒。行数必须与清理后的歌词行数完全一致。
3. 每句须满足 `0 <= start < end <= 歌曲时长`，句间不得重叠，时间必须单调；使用有限数值。
4. 若人工确认采用对齐时间轴，将顶层 `status` 设为 `aligned`，并将各行 `status` 设为 `aligned`（或删除行级状态以继承顶层状态）。若仍需草稿，使用 `estimated`。
5. 用同一歌词、同一音频和修正后的文件重跑，建议指定新输出目录以保留首次结果：

```powershell
scripts/run.ps1 -Lyrics <同一路径或文本> -Audio <同一音频> -Alignment <修正后的alignment.json> -OutputDir <新输出目录>
```

Python 对应参数为 `--alignment` 与 `--output-dir`。人工文件的顶层状态只接受 `aligned` 或 `estimated`。

人工修正只提供新的内部目标时间轴。生成器仍把后端记为 `user-provided`，且 `human_vocal_sync` 仍为 `unverified`；不会因为人工编辑就自动证明真实人声误差不超过 50ms。行级 `confidence` 也不应被当作人工验收证明。

## 6. 结果解读与退出码

- 退出码 `0`：自动 QC 通过。若 `generation.json` 中 `draft=true`，交付物仍是估计时间轴草稿。
- 退出码 `2`：已生成视频但 QC 未通过，视频保留为 `draft.mp4`；查看 `qc.json` 的 `errors`。
- 退出码 `1`：输入、依赖、对齐、渲染或封装等发生 fatal 错误，不能保证已有完整成片。
- 退出码 `130`：用户取消。

`passed=true` 仅表示自动检查通过，不等于 `draft=false`，更不等于真人声同步或主观艺术质量通过。

## 7. 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 提示缺少 FFmpeg/FFprobe | 安装并加入 `PATH`，重新打开终端后确认两者可执行。 |
| 无法准备 Python 3.12 | 安装 Python 3.12 或 `uv` 后重试；不要混用未安装锁定依赖的解释器。 |
| 依赖或模型下载失败 | 检查网络与缓存目录权限。对齐不可用时可降级为估计时间轴，但必须按草稿解读并查看警告。 |
| 找不到中文字体 | 安装受支持的中文黑体，或在自定义样式中设置 `subtitle.font_path`。 |
| 歌词为空、行数不一致或音频损坏 | 修正输入；人工对齐重跑还必须保持歌词与首次输入完全一致。 |
| 字幕过长或越界 | 缩短/拆分行，或高级调整字幕字号、行数和安全区；不得忽略 QC 的字幕错误。 |
| 音频偏移超过 50ms | 查看 `audio_offset_ms` 与测量置信度。生成器在测量足够可信时自动校正重封装一次，仍超限则判失败并保留草稿。 |
