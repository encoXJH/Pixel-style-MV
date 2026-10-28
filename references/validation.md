# 验证、50ms 门限与真实性边界

本文定义自动 QC 能证明什么、不能证明什么，以及报告字段的阅读方式。文档本身不构成任何模型接口测试或端到端测试已经通过的声明；实际结果必须以当次运行生成的报告为准。

## 1. 两种 50ms 指标

默认门限均为绝对值不超过 50ms，但两者都不是“真实人声同步误差”。

### 内部时间轴误差

对镜头、字幕和最终视频 PTS，比较目标秒时间与实际帧时间：

`error_ms = (actual_frame / fps - target_seconds) × 1000`

- 默认 `fps=30`，一帧约 33.333ms。
- 每句歌词只在 `timeline.json.lyric_cues` 中量化一次，字幕与句界镜头共享该 cue 的目标时间、实际帧号和稳定 ID；字幕用向上取整避免提前出现。
- 句界镜头禁止独立节拍吸附，固定 `beat_snap_ms=0.0`、`beat_snap_applied=false`；最终 MP4 的第 i 帧 PTS 还应与 `i/fps` 一致。
- 自动检查内部目标与量化/封装结果的偏差是否超过 50ms，同时检查单调性、缺帧、覆盖完整性及活动歌词帧的 `visual_line_id/lyric_cue_id/subtitle_id` 是否指向同一句。

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

统一歌词绑定还逐活动歌词帧核对视觉镜头、cue 与字幕是否属于同一句；天气检查覆盖六种状态、过渡区间、阶段数、分屏限制和句界禁止独立吸附。`lyric_frame_match_ratio=1.0` 表示内部 cue—镜头—字幕映射覆盖率为 100%，不表示真实人声同步已验证。

典型指标位于 `qc.json.metrics.timeline`：镜头/字幕数量、最大镜头误差、最大字幕误差、最大时间轴误差、转场计数、活动歌词帧数、歌词绑定匹配数/失配数、歌词帧匹配率和天气过渡统计。

### B. `frames_log`：逐帧渲染日志

逐行读取 `frames.jsonl`，检查缺失、重复、越界帧，PTS 单调性，镜头/字幕/转场映射，字幕溢出与对比度，以及亮度、异常黑帧和冻结代理指标。

逐帧绑定检查对比 `visual_line_id`、`lyric_cue_id`、`lyric_line_id`、`subtitle_id` 和 `local_frame`；天气检查对比目标态、前态、活动标记、阶段、总阶段数与 `weather_mix`。缺失帧也计入失配，重复帧不会重复增加匹配数。对应指标为 `lyric_frame_match_ratio`、`weather_frame_match_ratio` 和 `weather_transition_match_ratio`，三者通过只证明日志遵守内部时间轴。

渲染日志与最终编码视频是两类证据。日志缺少精确帧差时可以使用亮度、色板等辅助识别冻结，但最终 MP4 仍会执行解码帧差检查。

### C. `output`：最终候选 MP4

独立使用 FFprobe/FFmpeg：

- 探测 1920×1080、30fps、视频流和音轨；逐帧核对数量、尺寸、PTS 与 `i/fps`。
- 流式解码全部视频帧，检查异常黑帧、连续冻结和帧差突变的运动连续性。
- 解码原生分辨率底部字幕检测区域，检查活动帧的字幕检测区域/文字存在性和对比度；H.264 检查保留压缩容差，不与无损源帧逐字节比较。
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
| `lyric_binding_pass` | 时间轴与实际逐帧日志中的歌词 cue、视觉镜头和字幕绑定门禁。 |
| `weather_pass` | 天气词表、过渡参数及实际逐帧天气状态/阶段绑定门禁。 |
| `human_vocal_sync` | 始终为 `unverified`。 |
| `max_timeline_error_ms` | 三层报告中内部时间轴/PTS 误差的最大值。 |
| `audio_offset_ms` | 成片音轨相对源音频的偏移；正值为滞后。 |
| `correction` | 本次报告是否为自动音频校正后的复测。 |
| `errors` / `warnings` | 按 `timeline`、`frames_log`、`output` 前缀归因的错误与警告。 |
| `metrics.timeline` | 内部时间轴详细指标。 |
| `metrics.frames_log` | 渲染日志详细指标。 |
| `metrics.output` | 最终封装、全帧解码、字幕检测区域和音频测量详细指标。 |

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

## 6. 风格结构校验与艺术验收

正式生成在渲染前写入 `style-qc.json`，独立预览也会生成同名报告。`validate_style_profile()` 检查：

1. 参考风格档案是否存在，逐镜头叙事阶段、章节、运镜、调度、特效、声音 cue 和密度字段是否齐全；
2. 动作、特效和运镜是否属于当前已实现的词表，裁切/横移/保持帧是否为非负整数；
3. 物件镜头是否最多两件道具，情绪叠加是否不超过配置上限；
4. 所有转场是否存在于 `assets/style.json`，片头片尾是否回到同场景和主天气；
5. 三个及以上尾奏镜头是否具有场景、景别、动作、构图或天气差异，间奏/尾奏是否清空旧歌词 `line_id`；
6. 汇总实际出现的镜头类型、动作、特效、运镜、转场和声音 cue 覆盖清单。

结构校验失败会中止正式生成。通过时也只表示 `scope=structural_style_implementation_only`，并保留 `art_review_required=true`、`reference_audio_available=false`。它不验证主观相似度、原作者工程参数、参考混音或精确原始转场时序，也不保证每一种已实现元素都在某首特定歌曲中被歌词触发；应结合 `coverage` 判断当次覆盖范围。

## 7. 独立美术预览的边界

`scripts/preview.py` 用于场景、角色、镜头、动作、转场和字幕构图检查。它使用等分估计时间轴，不读取音频、不生成 MP4，也不执行本文所述的三层技术 QC。输出 `preview.json` 明确记录 `preview_only=true`、`timing_status=estimated_equal_duration`、`audio_sync=not_evaluated` 和 `human_vocal_sync=unverified`。

美术修改后应实际查看五类证据：代表帧与 `contact-sheet.jpg`、动作序列与 `motion-contact-sheet.jpg`、转场过程帧与 `transition-contact-sheet.jpg`、六类天气与 `weather-contact-sheet.jpg`、五组天气变化与 `weather-transition-contact-sheet.jpg`。检查角色造型和服装是否统一、动作是否可读、运镜是否保持硬边像素、转场与天气过渡是否连续、天气配色和粒子是否有区分度、场景细节与信息密度是否平衡、片头片尾是否呼应，以及字幕是否完整留在安全区。自动测试、结构校验和最终 MP4 的技术 QC 均不能代替人工艺术验收。

## 8. 声音设计与参考音频边界

正式生成额外输出 `sound-design.json` 和 `sound-cues.wav`。后者为 48kHz、立体声、16-bit PCM 的原创程序化环境与动作 cue；封装时用户歌曲仍是主音乐，cue 以低电平混入并触发轻度侧链闪避。报告记录 cue 类型、时刻、时长、增益和混音配置，禁用声音或空镜头时也应生成合法静音 WAV。

生成器不接受参考音轨输入；本次参考分析只用于画面与叙事，不用于复刻混音。因此 `original_reference_audio_available=false`、`music_source=user_song`、`design_status=original_programmatic_cues`，且 `generation.json.reference_mix_recreated=false`。不得把这些 cue 描述为原作者音效、原始分轨或参考混音的还原。自动音频偏移 QC 只比较最终音轨与用户源歌曲的封装关系，不证明声音设计的主观相似度；仍需人工试听歌曲主导性、cue 可辨识度、闪避是否自然和整体响度。
