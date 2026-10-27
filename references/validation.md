# 验证、50ms 门限与真实性边界

本文定义自动 QC 能证明什么、不能证明什么，以及报告字段的阅读方式。文档本身不构成任何模型接口测试或端到端测试已经通过的声明；实际结果必须以当次运行生成的报告为准。

## 1. 两种 50ms 指标

默认门限均为绝对值不超过 50ms，但两者都不是“真实人声同步误差”。

### 内部时间轴误差

对镜头、字幕和最终视频 PTS，比较目标秒时间与实际帧时间：

`error_ms = (actual_frame / fps - target_seconds) × 1000`

- 默认 `fps=30`，一帧约 33.333ms。
- 字幕目标来自 `alignment.json`，实际起止帧来自 `timeline.json`；字幕用向上取整避免提前出现。
- 镜头保存目标时间、实际帧号与节拍吸附信息；最终 MP4 的第 i 帧 PTS 还应与 `i/fps` 一致。
- 自动检查内部目标与量化/封装结果的偏差是否超过 50ms，同时检查单调性、缺帧和覆盖完整性。

这只能证明成片遵守了所给的内部时间轴。若输入时间轴本身偏离真实人声，内部误差再小也不会修正这一事实。

### 实际封装音频偏移

分别解码源音频与最终候选 MP4 的音轨，通过归一化互相关估计 `audio_offset_ms`：正值表示成片音频滞后，负值表示提前。验收要求 `abs(audio_offset_ms) <= 50`，并同时记录测量方法、置信度与警告。

当前测量默认最多使用音频前 90 秒。静音、常量信号、有效重叠不足或缺乏正相关峰会使测量不可靠；置信度为 0 或测量失败会使音频门禁失败，低置信警告必须保留。通过一次测量不等于证明任意段落的人声都已对齐。

当偏移超限且测量置信度至少为 0.5 时，生成器自动反向校正并重封装一次，再执行完整 QC；仍超限则失败并保留草稿。不得通过手工修改报告数值绕过复测。

### 与人声同步的区别

没有人工参考时间轴时，`human_vocal_sync` 必须为 `unverified`。当前 `alignment.json`、`qc.json` 和 `generation.json` 均始终保留该状态，包括人工修正 alignment 后重跑的情况。

因此不得宣称：

- 任意歌曲的真实人声误差必然不超过 50ms；
- 模型 `confidence` 高就等于真人声同步已验证；
- 节拍吸附成功就等于歌词贴人声；
- `qc.passed=true` 就等于存在人工人声参考或人工艺术验收。

## 2. 对齐状态与草稿门禁

`alignment.json` 顶层字段包括：

| 字段 | 含义 |
| --- | --- |
| `duration` | 用于对齐的歌曲时长，秒。 |
| `language` | 检测或模型返回的语言；可能为 `unknown`。 |
| `backend` | `stable-ts`、低置信时的 `stable-ts-low-confidence`、估计回退 `rms-length-estimator`、失败时的 `none` 或人工重跑时的 `user-provided`。 |
| `status` | 整体 `aligned`、`estimated` 或失败时的 `failed`。 |
| `confidence` | 行级模型/估计置信度的汇总；不是人声误差。估计行默认为 0。 |
| `human_vocal_sync` | 固定为 `unverified`。 |
| `warnings` | 模型不可用、降级、测量或其他需要人工知悉的情况。 |
| `lines` | 逐句 `id/text/start/end/status/confidence`。 |

自动对齐优先使用 stable-ts/faster-whisper 的给定歌词对齐。失败时记录原因，并按 RMS 与句长估计完整时间轴；当前未实现词级 ASR 字符匹配回退，也不会把误识别文本冒充用户歌词。

- `aligned` 只表示获得可用对齐时间轴，不表示已有外部真人声参考。
- 自动对齐逐句检查置信度：任一句缺失或低于 0.35，即使平均置信度较高，也会将全部结果标记为 `estimated`，不能仅凭平均值放行。
- 被判定为低置信或 `estimated` 的结果只能输出/交付 `draft.mp4`；人工时间轴中任一句为 `estimated` 也会触发整体草稿状态。
- `estimated` 即使全部内部 QC 通过，也仍是 `draft=true`；`passed=true` 不得被写成“正式成片且真人声同步通过”。
- `failed` 或没有有效歌词行会中止生成，不能伪装成成功出片。
- 强混响、多声部、说唱、重叠演唱、无清晰人声或模型不可用都可能需要草稿或人工修正。

## 3. QC 三层检查

### A. `timeline`：内部时间轴

检查帧率、尺寸、总帧数、镜头连续覆盖、目标与实际帧误差、字幕区间与单调性，以及转场类型、时长、位置和使用次数。帧区间均为左闭右开。

典型指标位于 `qc.json.metrics.timeline`：镜头/字幕数量、最大镜头误差、最大字幕误差、最大时间轴误差和转场计数。

### B. `frames_log`：逐帧渲染日志

逐行读取 `frames.jsonl`，检查缺失、重复、越界帧，PTS 单调性，镜头/字幕/转场映射，字幕溢出与对比度，以及亮度、异常黑帧和冻结代理指标。

渲染日志与最终编码视频是两类证据。日志缺少精确帧差时可以使用亮度、色板等辅助识别冻结，但最终 MP4 仍会执行解码帧差检查。

### C. `output`：最终候选 MP4

独立使用 FFprobe/FFmpeg：

- 探测 1920×1080、30fps、视频流和音轨；逐帧核对数量、尺寸、PTS 与 `i/fps`。
- 流式解码全部视频帧，检查异常黑帧、连续冻结和帧差突变的运动连续性。
- 解码原生分辨率底部字幕带，检查活动帧的字幕带/文字存在性和对比度；H.264 检查保留压缩容差，不与无损源帧逐字节比较。
- 解码源音频和成片音轨，测量实际封装偏移并记录置信度。
- 按默认间隔生成联系表，供人工快速复核，但联系表不是自动艺术评审。

## 4. `qc.json` 字段

| 字段 | 解读 |
| --- | --- |
| `passed` | 三层自动检查及门禁是否全部通过；不表示真人声或主观艺术质量通过。 |
| `draft` | 对齐状态不是 `aligned` 时为 `true`；QC 失败时最终交付也必须是草稿。 |
| `timeline_sync_pass` | 内部时间轴及 PTS 同步门禁。 |
| `audio_sync_pass` | 实际封装音频偏移测量与 50ms 门禁。 |
| `frame_integrity_pass` | 帧数、尺寸、覆盖、PTS、黑帧、冻结等完整性门禁。 |
| `subtitle_pass` | 字幕区间、映射、溢出、存在性与对比度门禁。 |
| `transition_pass` | 转场区间/标记及相关黑帧、帧差、冻结检查门禁。 |
| `human_vocal_sync` | 始终为 `unverified`。 |
| `max_timeline_error_ms` | 三层报告中内部时间轴/PTS 误差的最大值。 |
| `audio_offset_ms` | 成片音轨相对源音频的偏移；正值为滞后。 |
| `correction` | 本次报告是否为自动音频校正后的复测。 |
| `errors` / `warnings` | 按 `timeline`、`frames_log`、`output` 前缀归因的错误与警告。 |
| `metrics.timeline` | 内部时间轴详细指标。 |
| `metrics.frames_log` | 渲染日志详细指标。 |
| `metrics.output` | 最终封装、全帧解码、字幕带和音频测量详细指标。 |

音频测量补充字段为 `metrics.output.audio_offset_confidence`、`audio_offset_method`、`audio_offset_warning`；`audio_offset_positive_means=rendered_audio_lag` 定义正负号。`metrics.output.transition_check_scope=duration_markers_black_frames_difference_and_freeze_only` 明确限定转场自动检查范围。

`generation.json` 是交付摘要，应与 `qc.json` 一起阅读。重点检查 `output`、`passed`、`draft`、`alignment_status`、`audio_offset_ms`、`corrected` 和 `human_vocal_sync`，不要只看文件名或退出码。

## 5. 转场与人工复核边界

转场自动验证仅覆盖：

1. 帧区间和时长是否有效，类型、位置、次数是否符合配置；
2. 渲染帧标记是否与时间轴一致；
3. 是否存在非预期黑帧，`iris_out` 黑帧是否被单独识别；
4. 解码帧差和连续冻结是否触及运动连续性阈值。

它不证明转场“自然”、节奏“舒服”、叙事“准确”或画面与某位作者“完全一致”。这些结论需要人工观看、试听和必要的外部参考，不能由 `transition_pass` 推断。

如需验证真实人声，应另行建立可信的人工句级参考并逐句比较；当前流程不会把这类主观或外部验证自动写成 `verified`。人工修正 alignment 后仍须查看 `draft`/`final` 判定、QC 报告、联系表和关键帧，并保留 `human_vocal_sync=unverified` 的说明。
