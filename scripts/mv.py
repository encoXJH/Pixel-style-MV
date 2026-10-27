from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import media
import qc
import render


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 FFmpeg，请先将 ffmpeg 与 ffprobe 加入 PATH") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 FFmpeg：{exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg 执行失败：{detail or '未知错误'}")


def _mux_audio(
    video_only: Path,
    source_audio: Path,
    output: Path,
    style: dict,
    shift_seconds: float = 0.0,
) -> None:
    """封装原音频；shift_seconds 为正值时延后，为负值时提前。"""
    if not math.isfinite(shift_seconds):
        raise ValueError("音频校正量必须是有限数值")
    config = style["render"]
    sample_rate = int(config.get("audio_sample_rate", 48000))
    filters: list[str] = []
    if shift_seconds < 0:
        filters.extend(
            [
                f"atrim=start={abs(shift_seconds):.9f}",
                "asetpts=PTS-STARTPTS",
            ]
        )
    elif shift_seconds > 0:
        filters.append(f"adelay={shift_seconds * 1000.0:.6f}:all=1")
    filters.append(f"aresample={sample_rate}:async=1:first_pts=0")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_only),
            "-i",
            str(source_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            str(config.get("audio_bitrate", "256k")),
            "-ar",
            str(sample_rate),
            "-af",
            ",".join(filters),
            "-movflags",
            "+faststart",
            "-sn",
            "-dn",
            str(output),
        ]
    )


def _provided_alignment(
    path: Path,
    lyrics: list[str],
    duration: float,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取对齐文件 {path}：{exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("lines"), list):
        raise ValueError("对齐文件必须是包含 lines 数组的 JSON 对象")
    if len(value["lines"]) != len(lyrics):
        raise ValueError(f"对齐文件有 {len(value['lines'])} 行，但输入歌词有 {len(lyrics)} 行")

    status = str(value.get("status", "estimated"))
    if status not in {"aligned", "estimated"}:
        raise ValueError("人工对齐文件的 status 只能是 aligned 或 estimated")
    lines: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, (raw, text) in enumerate(zip(value["lines"], lyrics)):
        if not isinstance(raw, dict):
            raise ValueError(f"对齐文件第 {index + 1} 行不是对象")
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"对齐文件第 {index + 1} 行缺少有效 start/end") from exc
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"对齐文件第 {index + 1} 行时间不是有限数值")
        if start < 0 or end > duration + 1e-6 or end <= start:
            raise ValueError(f"对齐文件第 {index + 1} 行时间越界或句长无效")
        if index and start < previous_end:
            raise ValueError(f"对齐文件第 {index + 1} 行与上一行重叠或非单调")
        line_status = str(raw.get("status", status))
        if line_status not in {"aligned", "estimated"}:
            raise ValueError(f"对齐文件第 {index + 1} 行 status 只能是 aligned 或 estimated")
        try:
            confidence = float(
                raw.get("confidence", 1.0 if line_status == "aligned" else 0.0)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"对齐文件第 {index + 1} 行置信度必须是 [0, 1] 内的有限数值") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(f"对齐文件第 {index + 1} 行置信度必须是 [0, 1] 内的有限数值")
        lines.append(
            {
                "id": index,
                "text": text,
                "start": start,
                "end": min(duration, end),
                "status": line_status,
                "confidence": confidence,
            }
        )
        previous_end = end
    if any(line["status"] == "estimated" for line in lines):
        status = "estimated"
    return {
        "schema_version": 1,
        "duration": duration,
        "language": str(value.get("language", "unknown")),
        "backend": "user-provided",
        "status": status,
        "confidence": sum(line["confidence"] for line in lines) / len(lines),
        "human_vocal_sync": "unverified",
        "warnings": ["使用人工提供的句级时间轴；未自动核验其与真实人声的误差"],
        "lines": lines,
    }


def generate(
    lyrics_input: str,
    audio_input: str | Path,
    output_dir: str | Path | None = None,
    alignment_path: str | Path | None = None,
    style_path: str | Path | None = None,
) -> dict[str, Any]:
    audio = Path(audio_input).expanduser().resolve()
    if not audio.is_file():
        raise ValueError(f"歌曲音频不存在或不是文件：{audio}")
    lyrics = media.parse_lyrics(lyrics_input)
    style = render.load_style(style_path)
    render.find_font(style)
    probe = media.probe_media(audio)
    if not probe.get("audio"):
        raise ValueError("输入文件没有可解码音轨")
    duration = float(probe["duration"])
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else Path.cwd() / "outputs" / f"{audio.stem}-pixel-mv"
    )
    destination.mkdir(parents=True, exist_ok=True)
    work = destination / "work"
    work.mkdir(parents=True, exist_ok=True)

    print("[1/5] 分析节拍、onset 与能量…", flush=True)
    features = media.analyze_audio(audio)
    _write_json(destination / "audio-features.json", features)

    print("[2/5] 对齐逐句歌词…", flush=True)
    if alignment_path is not None:
        alignment = _provided_alignment(Path(alignment_path).expanduser().resolve(), lyrics, duration)
    else:
        alignment = media.align_lyrics(audio, lyrics, duration, features)
    if alignment.get("status") == "failed" or not alignment.get("lines"):
        raise RuntimeError("无法生成有效歌词时间轴：" + "；".join(alignment.get("warnings", [])))
    _write_json(destination / "alignment.json", alignment)
    timeline = render.plan_timeline(alignment, features, style)
    _write_json(destination / "timeline.json", timeline)

    print(f"[3/5] 渲染 {timeline['frame_count']} 帧像素画面…", flush=True)
    video_only = work / "video-only.mp4"
    render_result = render.render_video(
        timeline,
        style,
        video_only,
        destination / "frames.jsonl",
        destination / "keyframes",
    )

    print("[4/5] 封装原音频并执行全帧 QC…", flush=True)
    candidate = work / "muxed.mp4"
    _mux_audio(video_only, audio, candidate, style)
    initial_qc_path = work / "qc-initial.json"
    report = qc.run_qc(
        candidate,
        audio,
        timeline,
        style,
        destination / "frames.jsonl",
        work,
        initial_qc_path,
    )
    corrected = False
    offset = report.get("audio_offset_ms")
    confidence = report.get("metrics", {}).get("output", {}).get("audio_offset_confidence")
    max_audio_ms = float(style.get("qc", {}).get("max_audio_offset_ms", 50.0))
    if (
        isinstance(offset, (int, float))
        and math.isfinite(float(offset))
        and abs(float(offset)) > max_audio_ms
        and isinstance(confidence, (int, float))
        and float(confidence) >= 0.5
    ):
        print(f"      检测到音频偏移 {float(offset):.3f}ms，自动校正后复测…", flush=True)
        corrected_candidate = work / "muxed-corrected.mp4"
        _mux_audio(video_only, audio, corrected_candidate, style, -float(offset) / 1000.0)
        candidate = corrected_candidate
        corrected = True
        report = qc.run_qc(
            candidate,
            audio,
            timeline,
            style,
            destination / "frames.jsonl",
            work,
            destination / "qc.json",
            correction=True,
        )
    else:
        _write_json(destination / "qc.json", report)

    is_draft = bool(report.get("draft")) or not report.get("passed", False)
    final_path = destination / ("draft.mp4" if is_draft else "final.mp4")
    other_path = destination / ("final.mp4" if is_draft else "draft.mp4")
    if other_path.exists():
        other_path.unlink()
    os.replace(candidate, final_path)
    contact_sheet = qc.generate_contact_sheet(
        final_path,
        destination / "contact-sheet.jpg",
        float(style.get("qc", {}).get("contact_sheet_interval_seconds", 5)),
    )

    print("[5/5] 生成完成。", flush=True)
    result = {
        "output": str(final_path),
        "output_dir": str(destination),
        "qc": str(destination / "qc.json"),
        "contact_sheet": contact_sheet,
        "passed": bool(report.get("passed")),
        "draft": is_draft,
        "alignment_status": timeline.get("alignment_status"),
        "audio_offset_ms": report.get("audio_offset_ms"),
        "corrected": corrected,
        "frame_count": render_result["frame_count"],
        "human_vocal_sync": "unverified",
    }
    _write_json(destination / "generation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if not result["passed"]:
        print("QC 未通过，请查看 qc.json；视频已保留为 draft.mp4。", file=sys.stderr)
    elif result["draft"]:
        print("内部 QC 已通过，但歌词时间为估计值；真人声同步仍未验证。")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="仅需歌词与歌曲音频，生成统一像素叙事风 MV 并执行全帧 QC。"
    )
    parser.add_argument("--lyrics", required=True, help="UTF-8 歌词文件路径，或直接歌词文本")
    parser.add_argument("--audio", required=True, help="对应歌曲音频文件路径")
    parser.add_argument("--output-dir", help="可选输出目录；默认 outputs/歌曲名-pixel-mv")
    parser.add_argument("--alignment", help="可选人工修正 alignment.json，用于精确重跑")
    parser.add_argument("--style", help="可选 style.json；默认使用内置统一风格")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate(
            args.lyrics,
            args.audio,
            output_dir=args.output_dir,
            alignment_path=args.alignment,
            style_path=args.style,
        )
        return 0 if result["passed"] else 2
    except KeyboardInterrupt:
        print("已取消生成。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
