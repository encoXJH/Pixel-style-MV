---
name: pixel-narrative-mv
description: 仅凭歌词和歌曲音频生成统一像素叙事 MV，并完成歌词对齐、渲染、封装与 QC；用户要求用歌词和音频制作像素风、叙事或歌词 MV 时使用；不用于复制原作者素材或工程、承诺真人声同步精度或替代人工艺术评审。
---

# 像素叙事 MV

## 目标

把用户提供的歌词与对应歌曲音频直接变成可播放、可追踪、可质检的像素叙事 MV。正常流程只要求这两个输入，不要求参考视频、分镜、图片素材或额外风格配置。

采用本地程序化像素素材、自动歌词对齐、音频特征分析、确定性镜头规划、字幕合成和 FFmpeg 封装。默认不调用外部文生图服务，也不复制任何原作者的素材或工程。

## 正常执行

在 Skill 根目录运行唯一的正常两输入入口：

```powershell
scripts/run.ps1 -Lyrics <路径或文本> -Audio <音频>
```

Python 等价入口如下；其中 `python` 必须指向已安装 `requirements.txt` 锁定依赖的 Python 3.12 环境：

```powershell
python scripts/mv.py --lyrics <路径或文本> --audio <音频>
```

使用 `run.ps1` 创建的隔离环境时，将上例的 `python` 替换为 `.\.venv\Scripts\python.exe`。

1. 确认歌词与音频属于同一首歌；路径含空格或中文时使用引号。
2. 正常运行不要额外套用风格、对齐或输出参数。首次运行会准备隔离环境，模型首次使用可能需要下载。
3. 等待生成器完成，不以中途出现的文件判断成功。
4. 读取 `generation.json` 与 `qc.json`，检查 `output`、`passed`、`draft`、对齐状态、时间轴误差和实际音频偏移。
5. 交付成片及报告位置；草稿必须明确称为草稿，不得描述为真人声精准同步的完成版。

## 默认成片

- 工作画布为 480×270，按 4 倍最近邻放大到 1920×1080，30fps。
- 视频为 H.264，CRF 14、preset medium、yuv420p；音频为 AAC 256kbps、48kHz。
- 字幕在放大后的高分辨率画布合成；像素画面不使用运动模糊、光流或帧混合。
- 场景、道具、滤镜、动态、字幕和转场均遵循内置统一风格。

## 输出与交付判定

输出目录包含 `final.mp4` 或 `draft.mp4`，以及 `alignment.json`、`audio-features.json`、`timeline.json`、`frames.jsonl`、`qc.json`、`generation.json`、`contact-sheet.jpg` 和 `keyframes/`。

- 只有对齐状态为 `aligned` 且 QC 全部通过，才可交付 `final.mp4`。
- 被判定为低置信或 `estimated` 的对齐只能交付 `draft.mp4`；内部 QC 通过也不能抹去草稿属性。
- QC 未通过时同样保留 `draft.mp4` 和失败原因，不得改称其为正式成片。
- 可人工修正 `alignment.json` 后用 `-Alignment` 重跑；这不是正常流程的必填步骤，也不会自动验证真人声同步。

## 真实性边界

50ms 只是自动验收门限：自动流程只能强制检查最终候选成片相对内部时间轴的误差，以及实际封装音频相对源音频的偏移，二者均不得超过 50ms。它不等于任意歌曲的真实人声对齐误差必然不超过 50ms。

没有人工参考时间轴时，`human_vocal_sync` 永远为 `unverified`；当前生成流程也始终保留这一状态。模型置信度、节拍吸附或 `qc.passed=true` 都不是人声同步已验证的证明。

转场自动检查只覆盖帧区间、标记、异常黑帧以及帧差/冻结所反映的运动连续性，不证明转场在主观上自然，也不证明艺术质量。不得声称复制了原作者素材、工程或其作品艺术质量，也不得把尚未实际运行的模型测试或端到端测试说成已通过。

## 参考文档

- [两输入用法、参数、输出与人工重跑](references/usage.md)
- [场景、道具、滤镜、动态、字幕与转场规范](references/style-spec.md)
- [50ms 定义、QC 字段与不可保证项](references/validation.md)
