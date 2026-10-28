from __future__ import annotations

import io
import json
import math
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

import numpy as np

try:
    from . import media  # type: ignore
except (ImportError, ValueError):
    try:
        import media  # type: ignore
    except Exception:
        media = None  # type: ignore


_MAX_SYNC_MS = 50.0
_REPORT_FLAGS = (
    "timeline_sync_pass",
    "audio_sync_pass",
    "frame_integrity_pass",
    "subtitle_pass",
    "transition_pass",
    "lyric_binding_pass",
    "weather_pass",
)


class _Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.metrics: dict[str, Any] = {}
        self.flags = {name: True for name in _REPORT_FLAGS}
        self._error_count = 0
        self._warning_count = 0

    def error(self, message: str, *flags: str) -> None:
        self._error_count += 1
        if len(self.errors) < 100:
            self.errors.append(message)
        for flag in flags:
            self.flags[flag] = False

    def warning(self, message: str) -> None:
        self._warning_count += 1
        if len(self.warnings) < 100:
            self.warnings.append(message)

    def result(self) -> dict[str, Any]:
        errors = list(self.errors)
        warnings = list(self.warnings)
        if self._error_count > len(self.errors):
            errors.append(f"另有 {self._error_count - len(self.errors)} 条错误未展开")
        if self._warning_count > len(self.warnings):
            warnings.append(f"另有 {self._warning_count - len(self.warnings)} 条警告未展开")
        return {
            "passed": self._error_count == 0,
            **self.flags,
            "errors": errors,
            "warnings": warnings,
            "metrics": self.metrics,
        }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _exceeds(value: float, limit: float) -> bool:
    return value > limit + 1e-7


def validate_style_profile(timeline: dict[str, Any], style: dict[str, Any]) -> dict[str, Any]:
    """检查风格配置是否真正进入时间轴；不替代逐帧人工艺术评审。"""
    shots = _shots(timeline)
    profile = style.get("reference_profile", {})
    allowed_actions = set(profile.get("performance", {}).get("actions", []))
    allowed_effects = {"title_card", "photo_frame", "film_frame", "thought_bubble", "pixel_hearts", "fireworks", "stage_lights"}
    allowed_cameras = {"static", "push_in", "pull_out", "pan_left", "pan_right"}
    transitions = style.get("transitions", {})
    density = style.get("density", {})
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"id": name, "passed": bool(passed), "detail": detail})

    check("reference_profile", bool(profile.get("id")), str(profile.get("id", "missing")))
    check(
        "shot_direction_fields",
        all(all(key in shot for key in ("story_phase", "chapter", "camera", "blocking", "effects", "sfx", "density")) for shot in shots),
        "每个镜头必须包含叙事阶段、章节、运镜、调度、特效、声音 cue 与密度字段",
    )
    check(
        "action_vocabulary",
        all(shot.get("action") in allowed_actions for shot in shots),
        "动作必须来自参考证据支持的动作表",
    )
    check(
        "integer_camera",
        all(
            shot.get("camera", {}).get("kind") in allowed_cameras
            and all(isinstance(shot.get("camera", {}).get(key), int) and shot.get("camera", {}).get(key) >= 0 for key in ("max_crop_pixels", "pan_pixels", "hold_frames"))
            for shot in shots
        ),
        "运镜只允许静态、整数像素推拉和横移",
    )
    check(
        "effects_vocabulary",
        all(set(shot.get("effects", [])).issubset(allowed_effects) for shot in shots),
        "画面特效必须来自已实现的框中框、想象泡、爱心、烟花、标题与舞台灯光",
    )
    check(
        "information_density",
        all(
            len(shot.get("props", [])) <= int(density.get("max_object_props", 2)) if shot.get("shot_type") == "object" else True
            for shot in shots
        ) and all(int(shot.get("density", {}).get("emotional_overlays", 0)) <= int(density.get("max_emotional_overlays", 2)) for shot in shots),
        "物件镜头最多两件道具，情绪叠加层不超过配置上限",
    )
    check(
        "transition_definitions",
        all(shot.get("transition_in", "hard_cut") in transitions and shot.get("transition_out", "hard_cut") in transitions for shot in shots),
        "所有转场必须存在帧数与使用上限配置",
    )
    epilogue = [shot for shot in shots if shot.get("story_phase") == "epilogue"]
    epilogue_signatures = {
        (shot.get("scene"), shot.get("shot_type"), shot.get("action"), shot.get("blocking", {}).get("layout"), shot.get("weather"))
        for shot in epilogue
    }
    check(
        "epilogue_diversity",
        len(epilogue) <= 2 or len(epilogue_signatures) >= min(3, len(epilogue)),
        "三个及以上尾奏镜头不得只是同一场景、动作和构图的复制",
    )
    check(
        "montage_line_binding",
        all(shot.get("line_id") is None for shot in shots if shot.get("kind") in ("instrumental", "epilogue")),
        "间奏和尾奏不得残留上一句歌词的视觉 line_id",
    )
    opening = next((shot for shot in shots if shot.get("story_phase") == "prologue"), None)
    ending = next((shot for shot in reversed(shots) if shot.get("story_phase") == "epilogue"), None)
    check(
        "motif_loop",
        opening is None or ending is None or (opening.get("scene"), opening.get("weather")) == (ending.get("scene"), ending.get("weather")),
        "片头片尾须回到同场景与主天气",
    )
    coverage = {
        "shot_types": sorted({str(shot.get("shot_type")) for shot in shots}),
        "actions": sorted({str(shot.get("action")) for shot in shots}),
        "effects": sorted({str(effect) for shot in shots for effect in shot.get("effects", [])}),
        "cameras": sorted({str(shot.get("camera", {}).get("kind")) for shot in shots}),
        "transitions": sorted({str(shot.get("transition_in", "hard_cut")) for shot in shots} | {str(shot.get("transition_out", "hard_cut")) for shot in shots}),
        "sound_cues": sorted({str(cue) for shot in shots for cue in shot.get("sfx", [])}),
    }
    return {
        "schema_version": 1,
        "profile": profile.get("id"),
        "passed": all(item["passed"] for item in checks),
        "scope": "structural_style_implementation_only",
        "art_review_required": True,
        "reference_audio_available": False,
        "checks": checks,
        "coverage": coverage,
        "not_validated": ["subjective_similarity", "original_project_parameters", "reference_mix", "exact_original_transition_timing"],
    }


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _pick(mapping: Any, *names: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _deep_pick(row: dict[str, Any], *names: str) -> Any:
    containers: list[Any] = [row]
    for key in ("metrics", "frame_metrics", "subtitle_metrics", "subtitle"):
        value = row.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        value = _pick(container, *names)
        if value is not None:
            return value
    return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        if value.strip().lower() in {"true", "yes", "1"}:
            return True
        if value.strip().lower() in {"false", "no", "0", "none", ""}:
            return False
    return None


def _fps(timeline: dict[str, Any], style: dict[str, Any]) -> float:
    value = _number(timeline.get("fps"))
    if value is None:
        value = _number(_pick(style.get("render"), "fps"))
    return value if value is not None and value > 0 else 30.0


def _max_sync_ms(style: dict[str, Any]) -> float:
    value = _number(_pick(style.get("timing"), "max_sync_ms"))
    if value is None:
        value = _number(_pick(style.get("qc"), "max_sync_ms"))
    return value if value is not None and value >= 0 else _MAX_SYNC_MS


def _max_audio_ms(style: dict[str, Any]) -> float:
    value = _number(_pick(style.get("qc"), "max_audio_offset_ms"))
    return value if value is not None and value >= 0 else _MAX_SYNC_MS


def _shots(timeline: dict[str, Any]) -> list[Any]:
    value = timeline.get("shots")
    return value if isinstance(value, list) else []


def _subtitles(timeline: dict[str, Any]) -> list[Any]:
    value = timeline.get("subtitles")
    return value if isinstance(value, list) else []


def _shot_frames(shot: Any) -> tuple[int | None, int | None]:
    start = _integer(_pick(shot, "start_frame", "actual_start_frame", "from_frame"))
    end = _integer(_pick(shot, "end_frame", "actual_end_frame", "to_frame"))
    return start, end


def _target_seconds(item: Any, prefix: str, fps: float) -> float | None:
    value = _number(
        _pick(
            item,
            f"target_{prefix}",
            f"target_{prefix}_seconds",
            f"target_{prefix}_time",
            f"{prefix}_target",
            f"{prefix}_seconds",
            f"{prefix}_time",
            prefix,
        )
    )
    if value is not None:
        return value
    frame = _integer(_pick(item, f"target_{prefix}_frame"))
    return frame / fps if frame is not None and fps > 0 else None


def _actual_frame(item: Any, prefix: str) -> int | None:
    return _integer(
        _pick(
            item,
            f"actual_{prefix}_frame",
            f"{prefix}_frame",
            f"quantized_{prefix}_frame",
        )
    )


def _shot_id(shot: Any, index: int) -> str:
    value = _pick(shot, "id", "shot_id")
    return str(index if value is None else value)


def _subtitle_id(subtitle: Any, index: int) -> str:
    value = _pick(subtitle, "id", "subtitle_id", "line_id")
    return str(index if value is None else value)


def _shot_frame_map(timeline: dict[str, Any], frame_count: int) -> list[tuple[int, str] | None]:
    result: list[tuple[int, str] | None] = [None] * frame_count
    for index, shot in enumerate(_shots(timeline)):
        start, end = _shot_frames(shot)
        if start is None or end is None:
            continue
        for frame in range(max(0, start), min(frame_count, end)):
            result[frame] = (index, _shot_id(shot, index))
    return result


def _active_subtitle_map(timeline: dict[str, Any], frame_count: int) -> list[frozenset[str]]:
    empty = frozenset()
    result = [empty] * frame_count
    for index, subtitle in enumerate(_subtitles(timeline)):
        start = _actual_frame(subtitle, "start")
        end = _actual_frame(subtitle, "end")
        if start is None or end is None:
            continue
        subtitle_id = _subtitle_id(subtitle, index)
        for frame in range(max(0, start), min(frame_count, end)):
            result[frame] = frozenset((subtitle_id,))
    return result


def _transition_value(value: Any) -> tuple[str | None, int | None]:
    if isinstance(value, str):
        name = value.strip()
        return (None if not name or name.lower() == "none" else name), None
    if isinstance(value, dict):
        name = _pick(value, "type", "name", "kind", "transition")
        duration = _integer(_pick(value, "duration_frames", "frames"))
        return (str(name) if name is not None else None), duration
    return None, None


def _transition_schedule(
    timeline: dict[str, Any],
    style: dict[str, Any],
    frame_count: int,
    report: _Report | None = None,
) -> list[dict[str, Any]]:
    transition_style = style.get("transitions")
    if not isinstance(transition_style, dict):
        transition_style = {}
    definitions: list[tuple[Any, int | None, str]] = []
    top_level = timeline.get("transitions")
    if isinstance(top_level, list) and top_level:
        definitions.extend((item, None, "explicit") for item in top_level)
    else:
        shots = _shots(timeline)
        for index, shot in enumerate(shots):
            if not isinstance(shot, dict):
                continue
            if shot.get("transition") is not None:
                transition = shot.get("transition")
                if not isinstance(transition, dict) and shot.get("transition_frames") is not None:
                    transition = {"type": transition, "duration_frames": shot.get("transition_frames")}
                definitions.append((transition, index, "generic"))
            elif shot.get("transition_type") is not None:
                definitions.append(
                    (
                        {
                            "type": shot.get("transition_type"),
                            "duration_frames": shot.get("transition_duration_frames"),
                        },
                        index,
                        "generic",
                    )
                )
            if shot.get("transition_in") is not None:
                definitions.append(
                    (
                        {
                            "type": shot.get("transition_in"),
                            "duration_frames": shot.get("transition_frames"),
                        },
                        index,
                        "in",
                    )
                )
            if shot.get("transition_out") is not None:
                definitions.append(
                    (
                        {
                            "type": shot.get("transition_out"),
                            "duration_frames": shot.get("transition_out_frames"),
                        },
                        index,
                        "out",
                    )
                )

    entries: list[dict[str, Any]] = []
    shots = _shots(timeline)
    for value, shot_index, placement in definitions:
        transition_type, duration = _transition_value(value)
        if transition_type is None:
            if report is not None:
                report.error("转场缺少有效类型", "transition_pass")
            continue
        config = transition_style.get(transition_type)
        if not isinstance(config, dict):
            if report is not None:
                report.error(f"未知转场类型: {transition_type}", "transition_pass")
            continue
        expected_duration = _integer(config.get("duration_frames"))
        if expected_duration is None or expected_duration < 0:
            expected_duration = 0

        explicit_start = _integer(_pick(value, "start_frame", "from_frame"))
        explicit_end = _integer(_pick(value, "end_frame", "to_frame"))
        marker_frame = _integer(_pick(value, "frame", "frame_index"))
        if explicit_start is None:
            explicit_start = marker_frame
        if duration is None and explicit_start is not None and explicit_end is not None:
            duration = explicit_end - explicit_start
        if duration is None:
            duration = expected_duration
        if duration < 0:
            if report is not None:
                report.error(f"转场 {transition_type} 的帧数为负数", "transition_pass")
            continue

        shot_start = shot_end = None
        if shot_index is not None and 0 <= shot_index < len(shots):
            shot_start, shot_end = _shot_frames(shots[shot_index])
        if report is not None and duration != expected_duration:
            clamped_duration = None
            if shot_start is not None and shot_end is not None:
                clamped_duration = min(expected_duration, max(0, shot_end - shot_start - 1))
            if clamped_duration is None or duration != clamped_duration or clamped_duration == expected_duration:
                report.error(
                    f"转场 {transition_type} 帧数 {duration} 与样式值 {expected_duration} 不一致",
                    "transition_pass",
                )

        start = explicit_start
        end = explicit_end
        if start is None:
            if end is not None:
                start = end - duration
            elif placement == "out" or (placement == "generic" and transition_type == "iris_out"):
                start = shot_end - duration if shot_end is not None else None
            else:
                start = shot_start
        if end is None and start is not None:
            end = start + duration
        if start is None or end is None:
            if report is not None and duration > 0:
                report.error(f"转场 {transition_type} 缺少可定位的帧区间", "transition_pass")
            continue
        if explicit_start is not None and explicit_end is not None and explicit_end - explicit_start != duration:
            if report is not None:
                report.error(f"转场 {transition_type} 的起止帧与帧数不一致", "transition_pass")
        if start < 0 or end < start or (frame_count > 0 and end > frame_count):
            if report is not None:
                report.error(f"转场 {transition_type} 的帧区间越界: [{start}, {end})", "transition_pass")
        if config.get("ending_only"):
            is_last_shot = shot_index is not None and shot_index == len(shots) - 1
            if (shot_index is not None and not is_last_shot) or (
                shot_index is None and frame_count > 0 and end != frame_count
            ):
                if report is not None:
                    report.error(f"转场 {transition_type} 只能用于结尾", "transition_pass")
        entries.append(
            {
                "type": transition_type,
                "start": start,
                "end": end,
                "duration": duration,
                "shot_index": shot_index,
            }
        )

    if report is not None:
        for transition_type, config in transition_style.items():
            if not isinstance(config, dict):
                continue
            maximum = _integer(config.get("max_per_video"))
            if maximum is None:
                continue
            count = sum(entry["type"] == transition_type for entry in entries)
            if count > maximum:
                report.error(
                    f"转场 {transition_type} 使用 {count} 次，超过上限 {maximum}",
                    "transition_pass",
                )
    return entries


def _transition_frame_map(entries: list[dict[str, Any]], frame_count: int) -> list[str | None]:
    result: list[str | None] = [None] * frame_count
    for entry in entries:
        for frame in range(max(0, entry["start"]), min(frame_count, entry["end"])):
            result[frame] = entry["type"]
    return result


def _row_transition(row: dict[str, Any]) -> str | None:
    value = row.get("transition")
    if isinstance(value, bool) or value is None:
        value = _pick(row, "transition_type", "transition_name")
    transition_type, _ = _transition_value(value)
    if transition_type is not None and transition_type.lower() in {"none", "null"}:
        return None
    return transition_type


def _row_subtitle_ids(row: dict[str, Any]) -> tuple[set[str], bool]:
    for key in ("subtitle_ids", "subtitles"):
        if key in row:
            value = row[key]
            if value is None:
                return set(), True
            if not isinstance(value, (list, tuple, set)):
                value = [value]
            result: set[str] = set()
            for item in value:
                if isinstance(item, dict):
                    item = _pick(item, "id", "subtitle_id", "line_id")
                if item is not None:
                    result.add(str(item))
            return result, True
    for key in ("subtitle_id", "subtitle"):
        if key in row:
            value = row[key]
            if isinstance(value, dict):
                value = _pick(value, "id", "subtitle_id", "line_id")
            return ({str(value)} if value is not None else set()), True
    return set(), False


def _validate_lyric_binding_timeline(
    timeline: dict[str, Any],
    fps: float,
    frame_count: int,
    report: _Report,
) -> dict[str, Any]:
    cues = timeline.get("lyric_cues")
    subtitles = _subtitles(timeline)
    shots = _shots(timeline)
    metrics: dict[str, Any] = {
        "active_lyric_frame_count": 0,
        "matched_lyric_frame_count": 0,
        "lyric_binding_mismatch_count": 0,
        "lyric_frame_match_ratio": 0.0,
    }
    if not isinstance(cues, list) or not cues or len(cues) != len(subtitles):
        report.error("lyric_cues 必须与字幕逐句一一对应", "lyric_binding_pass", "timeline_sync_pass")
        return metrics
    sync = timeline.get("sync")
    expected_sync = {
        "frame_clock": "shared_lyric_cue_v1",
        "interval": "left_closed_right_open",
        "quantization": "ceil_target_seconds_times_fps",
        "independent_shot_snap": False,
        "human_vocal_sync": "unverified",
    }
    if not isinstance(sync, dict) or any(sync.get(key) != value for key, value in expected_sync.items()):
        report.error("sync 未声明统一歌词帧时钟或真人声同步边界", "lyric_binding_pass", "timeline_sync_pass")

    shot_map = _shot_frame_map(timeline, frame_count) if frame_count else []
    shot_by_id = {_shot_id(shot, index): shot for index, shot in enumerate(shots)}
    previous_start = -1
    for index, cue in enumerate(cues):
        if not isinstance(cue, dict):
            report.error(f"歌词 cue {index} 不是对象", "lyric_binding_pass")
            continue
        cue_id = str(cue.get("id", f"lyric_{index:04d}"))
        subtitle = subtitles[index]
        start, end = _actual_frame(cue, "start"), _actual_frame(cue, "end")
        target_start = _target_seconds(cue, "start", fps)
        target_end = _target_seconds(cue, "end", fps)
        if start is None or end is None or target_start is None or target_end is None:
            report.error(f"歌词 cue {cue_id} 缺少目标时间或实际帧区间", "lyric_binding_pass")
            continue
        expected_start = min(frame_count, max(0, math.ceil(target_start * fps)))
        expected_end = min(frame_count, max(0, math.ceil(target_end * fps)))
        if (
            start <= previous_start
            or end <= start
            or end > frame_count
            or (start, end) != (expected_start, expected_end)
        ):
            report.error(f"歌词 cue {cue_id} 未统一使用 ceil(target×fps) 或帧区间无效", "lyric_binding_pass", "timeline_sync_pass")
        previous_start = start
        expected_ids = (f"lyric_{index:04d}", f"subtitle_{index:04d}")
        if (cue.get("id"), cue.get("subtitle_id")) != expected_ids:
            report.error(f"歌词 cue {index} 的稳定 ID 或字幕引用无效", "lyric_binding_pass")
        if not isinstance(subtitle, dict):
            report.error(f"字幕 {index} 不是对象", "lyric_binding_pass", "subtitle_pass")
            continue
        shared_fields = ("line_id", "text", "target_start", "target_end", "start", "end", "start_frame", "end_frame")
        if (
            subtitle.get("id") != cue.get("subtitle_id")
            or subtitle.get("cue_id") != cue.get("id")
            or any(subtitle.get(field) != cue.get(field) for field in shared_fields)
        ):
            report.error(f"字幕 {index} 与唯一歌词 cue 的内容或时间不一致", "lyric_binding_pass", "subtitle_pass")
        shot_ids = cue.get("shot_ids")
        if not isinstance(shot_ids, list) or not shot_ids:
            report.error(f"歌词 cue {cue_id} 没有对应叙事镜头", "lyric_binding_pass")
            continue
        if any(str(shot_id) not in shot_by_id for shot_id in shot_ids):
            report.error(f"歌词 cue {cue_id} 引用了不存在的镜头", "lyric_binding_pass")
            continue
        first_shot = shot_by_id[str(shot_ids[0])]
        first_start, _ = _shot_frames(first_shot)
        if first_start != start:
            report.error(f"歌词 cue {cue_id} 的首镜头未从同一帧开始", "lyric_binding_pass", "timeline_sync_pass")

        mismatch = 0
        for frame in range(max(0, start), min(frame_count, end)):
            metrics["active_lyric_frame_count"] += 1
            expected = shot_map[frame] if shot_map else None
            shot = shots[expected[0]] if expected is not None else None
            valid = bool(
                shot is not None
                and expected[1] in {str(value) for value in shot_ids}
                and shot.get("line_id") == cue.get("line_id")
                and shot.get("lyric_cue_id") == cue.get("id")
            )
            if valid:
                metrics["matched_lyric_frame_count"] += 1
            else:
                mismatch += 1
        if mismatch:
            metrics["lyric_binding_mismatch_count"] += mismatch
            report.error(
                f"歌词 cue {cue_id} 有 {mismatch} 个活动帧未绑定到同一句叙事镜头",
                "lyric_binding_pass",
                "timeline_sync_pass",
            )
    active = metrics["active_lyric_frame_count"]
    metrics["lyric_frame_match_ratio"] = metrics["matched_lyric_frame_count"] / active if active else 0.0
    if active == 0:
        report.error("时间轴没有可验证的活动歌词帧", "lyric_binding_pass")
    return metrics


def _validate_weather_timeline(timeline: dict[str, Any], report: _Report) -> dict[str, int]:
    allowed = {"day", "cloudy", "rain", "snow", "night", "sunset"}
    transition_shots = 0
    transition_frames = 0
    for index, shot in enumerate(_shots(timeline)):
        if not isinstance(shot, dict):
            continue
        weather = shot.get("weather", "day")
        source = shot.get("weather_from")
        if weather not in allowed or (source is not None and source not in allowed):
            report.error(f"镜头 {_shot_id(shot, index)} 使用未知天气状态", "weather_pass")
        start, end = _shot_frames(shot)
        length = end - start if start is not None and end is not None else 0
        frames = _integer(shot.get("weather_transition_frames", 0))
        steps = _integer(shot.get("weather_transition_steps", 5))
        if frames is None or frames < 0 or frames > length or steps is None or steps < 1:
            report.error(f"镜头 {_shot_id(shot, index)} 的天气过渡帧数或阶段数无效", "weather_pass")
            continue
        if frames and (
            source is None
            or source == weather
            or shot.get("compare_weather")
        ):
            report.error(f"镜头 {_shot_id(shot, index)} 的天气过渡起止状态无效", "weather_pass")
            continue
        if frames:
            transition_shots += 1
            transition_frames += frames
        if shot.get("beat_snap_applied") or (_number(shot.get("beat_snap_ms", 0.0)) or 0.0) != 0.0:
            report.error(f"镜头 {_shot_id(shot, index)} 不得独立吸附歌词句界", "lyric_binding_pass", "timeline_sync_pass")
    return {
        "weather_transition_shot_count": transition_shots,
        "weather_transition_frame_count": transition_frames,
    }


def validate_timeline(timeline: dict[str, Any], style: dict[str, Any]) -> dict[str, Any]:
    """验证内部时间轴；所有帧区间均按左闭右开处理。"""
    report = _Report()
    if not isinstance(timeline, dict) or not isinstance(style, dict):
        report.error("timeline 和 style 必须是对象", "frame_integrity_pass", "timeline_sync_pass")
        return report.result()

    fps_value = _number(timeline.get("fps"))
    expected_fps = _number(_pick(style.get("render"), "fps")) or 30.0
    if fps_value is None or fps_value <= 0:
        report.error("timeline.fps 必须是正数", "frame_integrity_pass", "timeline_sync_pass")
        fps_value = expected_fps
    elif abs(fps_value - expected_fps) > 1e-6:
        report.error(
            f"timeline.fps={fps_value} 与样式 fps={expected_fps} 不一致",
            "frame_integrity_pass",
            "timeline_sync_pass",
        )
    fps = fps_value
    max_sync_ms = _max_sync_ms(style)
    frame_count = _integer(timeline.get("frame_count"))
    if frame_count is None or frame_count <= 0:
        report.error("timeline.frame_count 必须是正整数", "frame_integrity_pass")
        frame_count = 0
    if timeline.get("duration") is not None:
        duration = _number(timeline.get("duration"))
        if duration is None or duration <= 0:
            report.error("timeline.duration 必须是正数", "frame_integrity_pass")
        elif frame_count and frame_count != math.ceil(duration * fps):
            report.error(
                f"frame_count={frame_count} 与 ceil(duration×fps)={math.ceil(duration * fps)} 不一致",
                "frame_integrity_pass",
            )

    raw_shots = timeline.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        report.error("timeline.shots 必须是非空数组", "frame_integrity_pass")
    shot_errors: list[float] = []
    previous_end = 0
    for index, shot in enumerate(_shots(timeline)):
        if not isinstance(shot, dict):
            report.error(f"镜头 {index} 不是对象", "frame_integrity_pass")
            continue
        start, end = _shot_frames(shot)
        if start is None or end is None:
            report.error(f"镜头 {index} 缺少整数 start_frame/end_frame", "frame_integrity_pass")
            continue
        if start != previous_end:
            report.error(
                f"镜头 {index} 不连续：起始帧 {start}，应为 {previous_end}",
                "frame_integrity_pass",
            )
        if end <= start:
            report.error(f"镜头 {index} 的结束帧必须大于起始帧", "frame_integrity_pass")
        if frame_count and end > frame_count:
            report.error(f"镜头 {index} 超出 frame_count", "frame_integrity_pass")
        previous_end = end
        for prefix, actual in (("start", start), ("end", end)):
            target = _target_seconds(shot, prefix, fps)
            if target is None:
                continue
            error_ms = (actual / fps - target) * 1000.0
            shot_errors.append(abs(error_ms))
            if _exceeds(abs(error_ms), max_sync_ms):
                report.error(
                    f"镜头 {_shot_id(shot, index)} {prefix} 时间误差 {error_ms:.3f}ms 超过 {max_sync_ms}ms",
                    "timeline_sync_pass",
                )
    if _shots(timeline) and frame_count and previous_end != frame_count:
        report.error(
            f"最后一个镜头结束于 {previous_end}，与 frame_count={frame_count} 不一致",
            "frame_integrity_pass",
        )

    raw_subtitles = timeline.get("subtitles")
    if not isinstance(raw_subtitles, list):
        report.error("timeline.subtitles 必须是数组", "subtitle_pass")
    subtitle_errors: list[float] = []
    previous_target_start = -math.inf
    previous_actual_start = -1
    for index, subtitle in enumerate(_subtitles(timeline)):
        if not isinstance(subtitle, dict):
            report.error(f"字幕 {index} 不是对象", "subtitle_pass")
            continue
        subtitle_id = _subtitle_id(subtitle, index)
        target_start = _target_seconds(subtitle, "start", fps)
        target_end = _target_seconds(subtitle, "end", fps)
        actual_start = _actual_frame(subtitle, "start")
        actual_end = _actual_frame(subtitle, "end")
        if target_start is None or target_end is None:
            report.error(f"字幕 {subtitle_id} 缺少有效 target_start/target_end", "subtitle_pass")
        elif target_end <= target_start or target_start < 0:
            report.error(f"字幕 {subtitle_id} 的目标时间区间无效", "subtitle_pass")
        if actual_start is None or actual_end is None:
            report.error(f"字幕 {subtitle_id} 缺少实际起止帧", "subtitle_pass")
            continue
        if actual_start < 0 or actual_end <= actual_start or (frame_count and actual_end > frame_count):
            report.error(f"字幕 {subtitle_id} 的实际帧区间无效: [{actual_start}, {actual_end})", "subtitle_pass")
        if target_start is not None:
            if target_start < previous_target_start:
                report.error(f"字幕 {subtitle_id} 的目标时间非单调", "timeline_sync_pass", "subtitle_pass")
            previous_target_start = target_start
        if actual_start < previous_actual_start:
            report.error(f"字幕 {subtitle_id} 的实际帧号非单调", "timeline_sync_pass", "subtitle_pass")
        previous_actual_start = actual_start
        for prefix, target, actual in (
            ("start", target_start, actual_start),
            ("end", target_end, actual_end),
        ):
            if target is None:
                continue
            error_ms = (actual / fps - target) * 1000.0
            subtitle_errors.append(abs(error_ms))
            if _exceeds(abs(error_ms), max_sync_ms):
                report.error(
                    f"字幕 {subtitle_id} {prefix} 时间误差 {error_ms:.3f}ms 超过 {max_sync_ms}ms",
                    "timeline_sync_pass",
                    "subtitle_pass",
                )

    lyric_metrics = _validate_lyric_binding_timeline(timeline, fps, frame_count, report)
    weather_metrics = _validate_weather_timeline(timeline, report)

    transitions = _transition_schedule(timeline, style, frame_count, report)
    transition_counts: dict[str, int] = {}
    for entry in transitions:
        transition_counts[entry["type"]] = transition_counts.get(entry["type"], 0) + 1
    all_errors = shot_errors + subtitle_errors
    report.metrics.update(
        {
            "fps": fps,
            "frame_count": frame_count,
            "shot_count": len(_shots(timeline)),
            "subtitle_count": len(_subtitles(timeline)),
            "max_sync_ms": max_sync_ms,
            "max_shot_error_ms": max(shot_errors, default=0.0),
            "max_subtitle_error_ms": max(subtitle_errors, default=0.0),
            "max_timeline_error_ms": max(all_errors, default=0.0),
            "transition_counts": transition_counts,
            **lyric_metrics,
            **weather_metrics,
        }
    )
    return report.result()


def validate_frames_log(
    frames_log_path: str | Path,
    timeline: dict[str, Any],
    style: dict[str, Any],
) -> dict[str, Any]:
    """逐行验证渲染日志，不加载源视频帧。"""
    report = _Report()
    if not isinstance(timeline, dict) or not isinstance(style, dict):
        report.error("timeline 和 style 必须是对象", "frame_integrity_pass")
        return report.result()
    fps = _fps(timeline, style)
    frame_count = _integer(timeline.get("frame_count")) or 0
    max_sync_ms = _max_sync_ms(style)
    qc_style = style.get("qc") if isinstance(style.get("qc"), dict) else {}
    black_threshold = _number(qc_style.get("black_frame_mean_luma"))
    black_threshold = black_threshold if black_threshold is not None else 8.0
    black_max_ratio = _number(qc_style.get("black_frame_max_ratio"))
    black_max_ratio = black_max_ratio if black_max_ratio is not None else 0.002
    freeze_limit = _integer(qc_style.get("freeze_run_frames")) or 90
    max_difference = _number(qc_style.get("max_frame_difference"))
    max_difference = max_difference if max_difference is not None else 235.0
    subtitle_style = style.get("subtitle") if isinstance(style.get("subtitle"), dict) else {}
    min_contrast = _number(subtitle_style.get("min_contrast")) or 4.5

    shot_map = _shot_frame_map(timeline, frame_count) if frame_count else []
    subtitle_map = _active_subtitle_map(timeline, frame_count) if frame_count else []
    cue_map: list[dict[str, Any] | None] = [None] * frame_count
    raw_cues = timeline.get("lyric_cues")
    if isinstance(raw_cues, list):
        for cue in raw_cues:
            if not isinstance(cue, dict):
                continue
            start, end = _actual_frame(cue, "start"), _actual_frame(cue, "end")
            if start is None or end is None:
                continue
            for frame in range(max(0, start), min(frame_count, end)):
                cue_map[frame] = cue
    expected_active_lyric_frames = sum(cue is not None for cue in cue_map)
    expected_weather_transition_frames = 0
    for shot in _shots(timeline):
        if not isinstance(shot, dict):
            continue
        start, end = _shot_frames(shot)
        frames = _integer(shot.get("weather_transition_frames", 0)) or 0
        source = shot.get("weather_from")
        if (
            start is not None
            and end is not None
            and source
            and source != shot.get("weather", "day")
            and not shot.get("compare_weather")
        ):
            expected_weather_transition_frames += min(frames, max(0, end - start))
    transitions = _transition_schedule(timeline, style, frame_count)
    transition_map = _transition_frame_map(transitions, frame_count) if frame_count else []
    transition_types = set((style.get("transitions") or {}).keys())
    shot_boundaries = {
        start
        for shot in _shots(timeline)
        for start, _ in [_shot_frames(shot)]
        if start is not None
    }

    seen = bytearray(frame_count) if frame_count else None
    extra_seen: set[int] = set()
    row_count = 0
    duplicate_count = 0
    invalid_index_count = 0
    missing_pts_count = 0
    pts_error_count = 0
    non_monotonic_pts = 0
    shot_mismatch_count = 0
    subtitle_mismatch_count = 0
    lyric_binding_mismatch_count = 0
    matched_lyric_frames = 0
    seen_active_lyric_frames = 0
    weather_binding_mismatch_count = 0
    matched_weather_frames = 0
    matched_weather_transition_frames = 0
    seen_weather_transition_frames = 0
    overflow_count = 0
    low_contrast_count = 0
    missing_contrast_count = 0
    min_seen_contrast: float | None = None
    max_pts_error_ms = 0.0
    previous_pts: float | None = None
    previous_hash: Any = None
    previous_difference: float | None = None
    previous_freeze_proxy: tuple[Any, ...] | None = None
    max_frame_difference = 0.0
    large_difference_count = 0
    missing_luma_count = 0
    missing_difference_count = 0
    black_count = 0
    allowed_iris_black_count = 0
    unexpected_black_count = 0
    freeze_run = 0
    freeze_start = 0
    max_freeze_run = 0
    freeze_run_count = 0
    actual_transition_counts: dict[str, int] = {}

    try:
        handle = open(frames_log_path, "r", encoding="utf-8-sig")
    except OSError as exc:
        report.error(f"无法读取 frames.jsonl: {exc}", "frame_integrity_pass")
        return report.result()

    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row_count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                report.error(f"frames.jsonl 第 {line_number} 行不是有效 JSON: {exc.msg}", "frame_integrity_pass")
                continue
            if not isinstance(row, dict):
                report.error(f"frames.jsonl 第 {line_number} 行必须是对象", "frame_integrity_pass")
                continue
            frame = _integer(_pick(row, "frame", "frame_index", "index", "frame_number"))
            if frame is None or frame < 0:
                invalid_index_count += 1
                report.error(f"frames.jsonl 第 {line_number} 行帧号无效", "frame_integrity_pass")
                continue
            duplicate = False
            if seen is not None and frame < frame_count:
                if seen[frame]:
                    duplicate = True
                else:
                    seen[frame] = 1
            else:
                if frame in extra_seen:
                    duplicate = True
                extra_seen.add(frame)
            if duplicate:
                duplicate_count += 1

            pts = _number(_pick(row, "pts_seconds", "pts_time", "pts", "timestamp"))
            if pts is None and row.get("pts_ms") is not None:
                pts_ms = _number(row.get("pts_ms"))
                pts = pts_ms / 1000.0 if pts_ms is not None else None
            if pts is None:
                missing_pts_count += 1
            else:
                error_ms = abs(pts - frame / fps) * 1000.0
                max_pts_error_ms = max(max_pts_error_ms, error_ms)
                if _exceeds(error_ms, max_sync_ms):
                    pts_error_count += 1
                if previous_pts is not None and pts <= previous_pts:
                    non_monotonic_pts += 1
                previous_pts = pts

            expected_transition = transition_map[frame] if 0 <= frame < frame_count else None
            actual_transition = _row_transition(row)
            if actual_transition is not None:
                actual_transition_counts[actual_transition] = actual_transition_counts.get(actual_transition, 0) + 1
                if actual_transition not in transition_types:
                    report.error(
                        f"帧 {frame} 使用未知转场标记 {actual_transition}",
                        "transition_pass",
                    )
            if actual_transition != expected_transition:
                hard_cut_at_boundary = (
                    actual_transition == "hard_cut" and expected_transition is None and frame in shot_boundaries
                )
                if not hard_cut_at_boundary:
                    report.error(
                        f"帧 {frame} 转场标记 {actual_transition!r} 与时间轴 {expected_transition!r} 不一致",
                        "transition_pass",
                    )

            if 0 <= frame < frame_count and shot_map:
                expected_shot = shot_map[frame]
                actual_shot_value = _deep_pick(row, "shot_id", "shot")
                if isinstance(actual_shot_value, dict):
                    actual_shot_value = _pick(actual_shot_value, "id", "shot_id")
                if actual_shot_value is None and row.get("shot_index") is not None:
                    actual_shot_value = row.get("shot_index")
                    expected_shot_value = expected_shot[0] if expected_shot is not None else None
                else:
                    expected_shot_value = expected_shot[1] if expected_shot is not None else None
                if actual_shot_value is None or expected_shot_value is None or str(actual_shot_value) != str(expected_shot_value):
                    shot_mismatch_count += 1

                actual_subtitles, subtitle_field_present = _row_subtitle_ids(row)
                expected_subtitles = set(subtitle_map[frame])
                if not subtitle_field_present or actual_subtitles != expected_subtitles:
                    subtitle_mismatch_count += 1

                if not duplicate:
                    shots = _shots(timeline)
                    shot = shots[expected_shot[0]] if expected_shot is not None else None
                    cue = cue_map[frame]
                    shot_start, _ = _shot_frames(shot) if shot is not None else (None, None)
                    local_frame = frame - shot_start if shot_start is not None else None
                    binding_fields = (
                        "visual_line_id",
                        "lyric_cue_id",
                        "lyric_line_id",
                        "subtitle_id",
                        "local_frame",
                    )
                    expected_binding = {
                        "visual_line_id": shot.get("line_id") if shot is not None else None,
                        "lyric_cue_id": cue.get("id") if cue is not None else None,
                        "lyric_line_id": cue.get("line_id") if cue is not None else None,
                        "subtitle_id": cue.get("subtitle_id") if cue is not None else None,
                        "local_frame": local_frame,
                    }
                    binding_valid = all(field in row for field in binding_fields) and all(
                        row.get(field) == value for field, value in expected_binding.items()
                    )
                    if cue is not None:
                        seen_active_lyric_frames += 1
                        if binding_valid:
                            matched_lyric_frames += 1
                        else:
                            lyric_binding_mismatch_count += 1
                    elif not binding_valid:
                        lyric_binding_mismatch_count += 1

                    weather_fields = (
                        "weather",
                        "weather_from",
                        "weather_transition",
                        "weather_transition_stage",
                        "weather_transition_steps",
                        "weather_mix",
                    )
                    target_weather = shot.get("weather", "day") if shot is not None else None
                    source_weather = shot.get("weather_from") if shot is not None else None
                    weather_frames = _integer(shot.get("weather_transition_frames", 0)) or 0 if shot is not None else 0
                    weather_steps = max(1, _integer(shot.get("weather_transition_steps", 5)) or 5) if shot is not None else 5
                    weather_active = bool(
                        source_weather
                        and source_weather != target_weather
                        and not shot.get("compare_weather")
                        and local_frame is not None
                        and 0 <= local_frame < weather_frames
                    ) if shot is not None else False
                    weather_stage = (
                        min(weather_steps - 1, int(local_frame * weather_steps / weather_frames))
                        if weather_active
                        else weather_steps
                    )
                    weather_amount = (weather_stage + 1) / weather_steps if weather_active else 1.0
                    actual_weather_amount = _number(row.get("weather_mix"))
                    weather_valid = (
                        all(field in row for field in weather_fields)
                        and row.get("weather") == target_weather
                        and row.get("weather_from") == source_weather
                        and _boolean(row.get("weather_transition")) is weather_active
                        and _integer(row.get("weather_transition_stage")) == weather_stage
                        and _integer(row.get("weather_transition_steps")) == weather_steps
                        and actual_weather_amount is not None
                        and abs(actual_weather_amount - weather_amount) <= 1e-6
                    )
                    if weather_valid:
                        matched_weather_frames += 1
                        if weather_active:
                            matched_weather_transition_frames += 1
                    else:
                        weather_binding_mismatch_count += 1
                    if weather_active:
                        seen_weather_transition_frames += 1

                overflow = _deep_pick(row, "subtitle_overflow", "subtitle_overflow_pixels", "overflow")
                overflow_bool = _boolean(overflow)
                overflow_number = _number(overflow)
                if overflow_bool is True or (overflow_bool is None and overflow_number is not None and overflow_number > 0):
                    overflow_count += 1
                if actual_subtitles or expected_subtitles:
                    contrast = _number(
                        _deep_pick(row, "subtitle_contrast", "subtitle_contrast_ratio", "contrast_ratio", "contrast")
                    )
                    if contrast is None:
                        missing_contrast_count += 1
                    else:
                        min_seen_contrast = contrast if min_seen_contrast is None else min(min_seen_contrast, contrast)
                        if contrast + 1e-9 < min_contrast:
                            low_contrast_count += 1

            luma = _number(_deep_pick(row, "mean_luma", "luma_mean", "luma"))
            black_value = _boolean(_deep_pick(row, "black_frame", "is_black", "source_black"))
            if luma is None and black_value is None:
                missing_luma_count += 1
            is_black = black_value if luma is None else luma < black_threshold
            if is_black:
                black_count += 1
                if actual_transition == "iris_out" or expected_transition == "iris_out":
                    allowed_iris_black_count += 1
                else:
                    unexpected_black_count += 1

            difference = _number(
                _deep_pick(row, "frame_difference", "frame_diff", "mean_frame_difference", "difference", "diff")
            )
            frame_hash = _deep_pick(row, "frame_hash", "pixel_hash")
            frozen = False
            if difference is not None:
                previous_freeze_proxy = None
                if difference < 0:
                    report.error(f"帧 {frame} 的帧差不能为负数", "frame_integrity_pass")
                else:
                    if previous_difference is not None or row_count > 1:
                        max_frame_difference = max(max_frame_difference, difference)
                        if difference > max_difference:
                            large_difference_count += 1
                    frozen = difference <= 1e-6
                previous_difference = difference
            elif frame_hash is not None:
                previous_freeze_proxy = None
                frozen = previous_hash is not None and frame_hash == previous_hash
            else:
                missing_difference_count += 1
                freeze_proxy: tuple[Any, ...] | None = None
                if luma is not None:
                    proxy_subtitles, _ = _row_subtitle_ids(row)
                    freeze_proxy = (
                        luma,
                        _deep_pick(row, "palette_size", "palette_count"),
                        _deep_pick(row, "shot_id", "shot"),
                        tuple(sorted(proxy_subtitles)),
                        actual_transition,
                    )
                    frozen = previous_freeze_proxy is not None and freeze_proxy == previous_freeze_proxy
                previous_freeze_proxy = freeze_proxy
            previous_hash = frame_hash

            if frozen:
                if freeze_run == 0:
                    freeze_start = frame
                freeze_run += 1
                max_freeze_run = max(max_freeze_run, freeze_run)
                if freeze_run == freeze_limit:
                    freeze_run_count += 1
                    report.error(
                        f"帧 {freeze_start} 起出现异常冻结，连续 {freeze_run} 帧",
                        "frame_integrity_pass",
                        "transition_pass",
                    )
            else:
                freeze_run = 0

    lyric_binding_mismatch_count += max(0, expected_active_lyric_frames - seen_active_lyric_frames)
    weather_binding_mismatch_count += max(0, frame_count - matched_weather_frames - weather_binding_mismatch_count)
    if row_count == 0:
        report.error("frames.jsonl 没有帧记录", "frame_integrity_pass")
    if frame_count:
        missing_count = frame_count - sum(seen or ())
        if missing_count:
            samples: list[int] = []
            for frame in range(frame_count):
                if not seen[frame]:
                    samples.append(frame)
                    if len(samples) == 10:
                        break
            report.error(
                f"frames.jsonl 缺少 {missing_count} 帧，示例: {samples}",
                "frame_integrity_pass",
            )
        out_of_range_count = sum(frame >= frame_count for frame in extra_seen)
        if out_of_range_count:
            report.error(f"frames.jsonl 含 {out_of_range_count} 个越界帧号", "frame_integrity_pass")
    if invalid_index_count:
        report.metrics["invalid_index_count"] = invalid_index_count
    if duplicate_count:
        report.error(f"frames.jsonl 有 {duplicate_count} 条重复帧记录", "frame_integrity_pass")
    if missing_pts_count:
        report.error(f"有 {missing_pts_count} 帧缺少有效 PTS", "timeline_sync_pass", "frame_integrity_pass")
    if pts_error_count:
        report.error(
            f"有 {pts_error_count} 帧 PTS 与 frame/fps 的偏差超过 {max_sync_ms}ms",
            "timeline_sync_pass",
        )
    if non_monotonic_pts:
        report.error(f"有 {non_monotonic_pts} 处 PTS 非严格递增", "timeline_sync_pass", "frame_integrity_pass")
    if shot_mismatch_count:
        report.error(f"有 {shot_mismatch_count} 帧的镜头映射与时间轴不一致", "frame_integrity_pass")
    if subtitle_mismatch_count:
        report.error(f"有 {subtitle_mismatch_count} 帧的字幕映射与时间轴不一致", "subtitle_pass")
    if lyric_binding_mismatch_count:
        report.error(
            f"有 {lyric_binding_mismatch_count} 帧的歌词 cue—镜头—字幕绑定缺失或不一致",
            "lyric_binding_pass",
            "timeline_sync_pass",
        )
    if not expected_active_lyric_frames:
        report.error("frames.jsonl 没有可验证的活动歌词帧", "lyric_binding_pass")
    if weather_binding_mismatch_count:
        report.error(
            f"有 {weather_binding_mismatch_count} 帧的天气状态或过渡阶段不一致",
            "weather_pass",
        )
    if overflow_count:
        report.error(f"有 {overflow_count} 个字幕活动帧发生 overflow", "subtitle_pass")
    if missing_contrast_count:
        report.error(f"有 {missing_contrast_count} 个字幕活动帧缺少对比度指标", "subtitle_pass")
    if low_contrast_count:
        report.error(
            f"有 {low_contrast_count} 个字幕活动帧对比度低于 {min_contrast}",
            "subtitle_pass",
        )
    if missing_luma_count:
        report.error(f"有 {missing_luma_count} 帧缺少 luma/黑帧指标", "frame_integrity_pass")
    if missing_difference_count:
        report.warning(
            f"有 {missing_difference_count} 帧缺少 frame_difference/frame_hash；"
            "日志侧使用 mean_luma/palette_size 辅助识别冻结，成片解码仍执行精确帧差检查"
        )
    if large_difference_count:
        report.error(
            f"有 {large_difference_count} 帧帧差超过 {max_difference}",
            "frame_integrity_pass",
            "transition_pass",
        )

    unexpected_black_ratio = unexpected_black_count / row_count if row_count else 0.0
    if unexpected_black_count:
        message = (
            f"发现 {unexpected_black_count} 个非 iris_out 黑帧，"
            f"占比 {unexpected_black_ratio:.6f}"
        )
        if unexpected_black_ratio > black_max_ratio:
            report.error(message + f"，超过阈值 {black_max_ratio}", "frame_integrity_pass", "transition_pass")
        else:
            report.warning(message + f"，未超过阈值 {black_max_ratio}")

    report.metrics.update(
        {
            "row_count": row_count,
            "unique_frame_count": (sum(seen or ()) + len(extra_seen)) if frame_count else len(extra_seen),
            "duplicate_frame_count": duplicate_count,
            "black_frame_count": black_count,
            "allowed_iris_out_black_frames": allowed_iris_black_count,
            "unexpected_black_frame_count": unexpected_black_count,
            "unexpected_black_frame_ratio": unexpected_black_ratio,
            "freeze_run_count": freeze_run_count,
            "max_freeze_run_frames": max_freeze_run,
            "freeze_proxy_frame_count": missing_difference_count,
            "max_frame_difference": max_frame_difference,
            "max_timeline_error_ms": max_pts_error_ms,
            "min_subtitle_contrast": min_seen_contrast,
            "actual_transition_frame_counts": actual_transition_counts,
            "active_lyric_frame_count": expected_active_lyric_frames,
            "logged_active_lyric_frame_count": seen_active_lyric_frames,
            "matched_lyric_frame_count": matched_lyric_frames,
            "lyric_binding_mismatch_count": lyric_binding_mismatch_count,
            "lyric_frame_match_ratio": (
                matched_lyric_frames / expected_active_lyric_frames
                if expected_active_lyric_frames
                else 0.0
            ),
            "weather_transition_frame_count": expected_weather_transition_frames,
            "logged_weather_transition_frame_count": seen_weather_transition_frames,
            "matched_weather_transition_frame_count": matched_weather_transition_frames,
            "weather_binding_mismatch_count": weather_binding_mismatch_count,
            "weather_frame_match_ratio": matched_weather_frames / frame_count if frame_count else 0.0,
            "weather_transition_match_ratio": (
                matched_weather_transition_frames / expected_weather_transition_frames
                if expected_weather_transition_frames
                else 1.0
            ),
        }
    )
    return report.result()


def _run_external(args: list[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"未找到外部程序 {args[0]}，请确认 FFmpeg/FFprobe 已加入 PATH") from exc
    except OSError as exc:
        raise RuntimeError(f"无法执行 {args[0]}: {exc}") from exc


def _probe_media(path: str | Path) -> dict[str, Any]:
    result = _run_external(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "未知 FFprobe 错误"
        raise RuntimeError(f"FFprobe 无法读取 {path}: {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe 返回了无效 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"FFprobe 返回格式异常: {path}")
    return value


def _probe_video_frames(path: str | Path) -> subprocess.CompletedProcess[Any]:
    return _run_external(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,pts_time,width,height",
            "-of",
            "compact=p=0:nk=0",
            str(path),
        ]
    )


def _parse_rate(value: Any) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            result = float(numerator) / denominator_value
        else:
            result = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decoded_rgb_frames(
    video_path: str | Path,
    width: int,
    height: int,
    filter_graph: str,
) -> Iterator[bytes]:
    """以生成器输出固定大小 RGB 帧，并在迭代结束时检查 FFmpeg 状态。"""
    frame_size = width * height * 3
    args = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filter_graph,
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 ffmpeg，请确认 FFmpeg 已加入 PATH") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 FFmpeg 解码: {exc}") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_chunks: list[bytes] = []

    def collect_stderr() -> None:
        stderr_chunks.append(process.stderr.read())

    stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
    stderr_thread.start()
    try:
        while True:
            data = _read_exact(process.stdout, frame_size)
            if not data:
                break
            if len(data) != frame_size:
                raise RuntimeError(f"FFmpeg 返回了不完整 RGB 帧：{len(data)}/{frame_size} 字节")
            yield data
    finally:
        process.stdout.close()
        return_code = process.wait()
        stderr_thread.join()
        process.stderr.close()
    stderr_bytes = b"".join(stderr_chunks)
    if return_code != 0:
        detail = stderr_bytes.decode("utf-8", errors="replace").strip() or "未知 FFmpeg 解码错误"
        raise RuntimeError(f"FFmpeg 解码失败: {detail}")


def _relative_luminance(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    rgb = frame.reshape(height, width, 3)
    values = np.arange(256, dtype=np.float64) / 255.0
    linear = np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)
    return (
        linear[rgb[..., 0]] * 0.2126
        + linear[rgb[..., 1]] * 0.7152
        + linear[rgb[..., 2]] * 0.0722
    )


def _subtitle_band_check(raw: bytes, width: int, height: int) -> tuple[float, float, int]:
    pixels = np.frombuffer(raw, dtype=np.uint8)
    luminance = _relative_luminance(pixels, width, height)
    light_mask = luminance >= 0.55
    dark_mask = luminance <= 0.12
    light_count = int(np.count_nonzero(light_mask))
    dark_count = int(np.count_nonzero(dark_mask))
    light_fraction = light_count / luminance.size
    if not light_count or not dark_count:
        return 0.0, light_fraction, dark_count
    light_level = float(np.median(luminance[light_mask]))
    dark_level = float(np.median(luminance[dark_mask]))
    contrast = (light_level + 0.05) / (dark_level + 0.05)
    return contrast, light_fraction, dark_count


def _offset_ms(value: Any) -> float | None:
    if isinstance(value, dict):
        value = _pick(value, "offset_ms", "audio_offset_ms", "measured_offset_ms")
    elif isinstance(value, (tuple, list)) and value:
        value = value[0]
    return _number(value)


def _measure_audio(
    source_audio_path: str | Path,
    video_path: str | Path,
    work_dir: str | Path,
) -> tuple[float, dict[str, Any]]:
    if media is None or not hasattr(media, "measure_audio_offset"):
        raise RuntimeError("无法导入同级 media.measure_audio_offset，未测量音频偏移")
    function = media.measure_audio_offset
    try:
        import inspect

        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: dict[str, Any] = {}
    if "work_dir" in parameters:
        kwargs["work_dir"] = str(work_dir)
    value = function(str(source_audio_path), str(video_path), **kwargs)
    offset = _offset_ms(value)
    if offset is None:
        raise RuntimeError("media.measure_audio_offset 未返回以毫秒表示的有限数值")
    metadata = dict(value) if isinstance(value, dict) else {}
    return offset, metadata


def validate_output(
    video_path: str | Path,
    source_audio_path: str | Path,
    timeline: dict[str, Any],
    style: dict[str, Any],
    work_dir: str | Path,
    correction: bool = False,
) -> dict[str, Any]:
    """独立探测并流式解码成片；correction 仅记录是否为校正后复测。"""
    report = _Report()
    report.metrics["correction"] = bool(correction)
    audio_offset_ms: float | None = None
    audio_measurement: dict[str, Any] = {}
    if not isinstance(timeline, dict) or not isinstance(style, dict):
        report.error("timeline 和 style 必须是对象", "frame_integrity_pass", "audio_sync_pass")
        result = report.result()
        result["audio_offset_ms"] = None
        return result
    video_path = Path(video_path)
    source_audio_path = Path(source_audio_path)
    if not video_path.is_file():
        report.error(f"成片不存在或不是文件: {video_path}", "frame_integrity_pass", "audio_sync_pass")
        result = report.result()
        result["audio_offset_ms"] = None
        return result

    expected_frame_count = _integer(timeline.get("frame_count")) or 0
    expected_fps = _fps(timeline, style)
    render_style = style.get("render") if isinstance(style.get("render"), dict) else {}
    expected_width = _integer(render_style.get("width")) or 1280
    expected_height = _integer(render_style.get("height")) or 720
    low_width = _integer(render_style.get("low_width")) or 320
    low_height = _integer(render_style.get("low_height")) or 180
    max_sync_ms = _max_sync_ms(style)
    qc_style = style.get("qc") if isinstance(style.get("qc"), dict) else {}
    black_threshold = _number(qc_style.get("black_frame_mean_luma"))
    black_threshold = black_threshold if black_threshold is not None else 8.0
    black_max_ratio = _number(qc_style.get("black_frame_max_ratio"))
    black_max_ratio = black_max_ratio if black_max_ratio is not None else 0.002
    freeze_limit = _integer(qc_style.get("freeze_run_frames")) or 90
    max_difference = _number(qc_style.get("max_frame_difference"))
    max_difference = max_difference if max_difference is not None else 235.0

    try:
        probe = _probe_media(video_path)
    except RuntimeError as exc:
        report.error(str(exc), "frame_integrity_pass", "audio_sync_pass")
        result = report.result()
        result["audio_offset_ms"] = None
        return result
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    video_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]
    if not video_streams:
        report.error("成片没有视频流", "frame_integrity_pass", "audio_sync_pass")
        result = report.result()
        result["audio_offset_ms"] = None
        return result
    video_stream = video_streams[0]
    actual_width = _integer(video_stream.get("width"))
    actual_height = _integer(video_stream.get("height"))
    if actual_width != expected_width or actual_height != expected_height:
        report.error(
            f"成片尺寸 {actual_width}x{actual_height} 与期望 {expected_width}x{expected_height} 不一致",
            "frame_integrity_pass",
        )
    actual_fps = _parse_rate(video_stream.get("avg_frame_rate")) or _parse_rate(video_stream.get("r_frame_rate"))
    if actual_fps is None:
        report.error("无法确定成片帧率", "frame_integrity_pass")
    elif abs(actual_fps - expected_fps) > 1e-6:
        report.error(
            f"成片帧率 {actual_fps} 与期望 {expected_fps} 不一致",
            "frame_integrity_pass",
            "timeline_sync_pass",
        )

    probed_frame_count = 0
    missing_pts = 0
    non_monotonic_pts = 0
    bad_frame_dimensions = 0
    max_pts_error_ms = 0.0
    pts_over_limit = 0
    previous_pts: float | None = None
    try:
        frame_probe = _probe_video_frames(video_path)
        if frame_probe.returncode != 0:
            raise RuntimeError(frame_probe.stderr.strip() or "FFprobe 逐帧探测失败")
        for line in frame_probe.stdout.splitlines():
            fields: dict[str, str] = {}
            for part in line.split("|"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    fields[key] = value
            if not fields:
                continue
            probed_frame_count += 1
            pts_value = fields.get("best_effort_timestamp_time") or fields.get("pts_time")
            pts = _number(pts_value)
            index = probed_frame_count - 1
            if pts is None:
                missing_pts += 1
            else:
                error_ms = abs(pts - index / expected_fps) * 1000.0
                max_pts_error_ms = max(max_pts_error_ms, error_ms)
                if _exceeds(error_ms, max_sync_ms):
                    pts_over_limit += 1
                if previous_pts is not None and pts <= previous_pts:
                    non_monotonic_pts += 1
                previous_pts = pts
            frame_width = _integer(fields.get("width"))
            frame_height = _integer(fields.get("height"))
            if frame_width is not None and frame_height is not None:
                if frame_width != expected_width or frame_height != expected_height:
                    bad_frame_dimensions += 1
    except (RuntimeError, OSError) as exc:
        report.error(f"逐帧 FFprobe 失败: {exc}", "frame_integrity_pass", "timeline_sync_pass")
    if probed_frame_count == 0:
        report.error("FFprobe 未返回任何视频帧", "frame_integrity_pass", "timeline_sync_pass")
    if expected_frame_count and probed_frame_count != expected_frame_count:
        report.error(
            f"FFprobe 帧数 {probed_frame_count} 与 timeline.frame_count={expected_frame_count} 不一致",
            "frame_integrity_pass",
        )
    if missing_pts:
        report.error(f"有 {missing_pts} 个视频帧缺少 PTS", "timeline_sync_pass", "frame_integrity_pass")
    if non_monotonic_pts:
        report.error(f"成片 PTS 有 {non_monotonic_pts} 处非严格递增", "timeline_sync_pass", "frame_integrity_pass")
    if pts_over_limit:
        report.error(
            f"有 {pts_over_limit} 帧 PTS 与 i/fps 的偏差超过 {max_sync_ms}ms",
            "timeline_sync_pass",
        )
    if bad_frame_dimensions:
        report.error(f"有 {bad_frame_dimensions} 个视频帧尺寸异常", "frame_integrity_pass")

    transitions = _transition_schedule(timeline, style, expected_frame_count)
    transition_map = _transition_frame_map(transitions, expected_frame_count) if expected_frame_count else []
    decoded_frame_count = 0
    previous: np.ndarray | None = None
    max_decoded_difference = 0.0
    large_difference_count = 0
    black_count = 0
    allowed_iris_black_count = 0
    unexpected_black_count = 0
    freeze_run = 0
    max_freeze_run = 0
    freeze_run_count = 0
    min_luma: float | None = None
    max_luma: float | None = None
    try:
        frames = _decoded_rgb_frames(
            video_path,
            low_width,
            low_height,
            f"scale={low_width}:{low_height}:flags=area",
        )
        for raw in frames:
            index = decoded_frame_count
            decoded_frame_count += 1
            current = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int16)
            channel_means = current.mean(axis=0, dtype=np.float64)
            luma = float(channel_means @ np.array([0.2126, 0.7152, 0.0722]))
            min_luma = luma if min_luma is None else min(min_luma, luma)
            max_luma = luma if max_luma is None else max(max_luma, luma)
            difference: float | None = None
            if previous is not None:
                difference = float(np.abs(current - previous).mean())
                max_decoded_difference = max(max_decoded_difference, difference)
                if difference > max_difference:
                    large_difference_count += 1
            is_black = luma < black_threshold
            if is_black:
                black_count += 1
                transition = transition_map[index] if 0 <= index < expected_frame_count else None
                if transition == "iris_out":
                    allowed_iris_black_count += 1
                else:
                    unexpected_black_count += 1
            if previous is None:
                freeze_run = 1
            elif difference is not None and difference <= 1e-6:
                freeze_run += 1
                if freeze_run == freeze_limit:
                    freeze_run_count += 1
            else:
                freeze_run = 1
            max_freeze_run = max(max_freeze_run, freeze_run)
            previous = current
    except (RuntimeError, OSError, ValueError) as exc:
        report.error(f"低画布全帧解码失败: {exc}", "frame_integrity_pass", "transition_pass")
    if decoded_frame_count == 0:
        report.error("FFmpeg 未解码出任何视频帧", "frame_integrity_pass")
    if probed_frame_count and decoded_frame_count != probed_frame_count:
        report.error(
            f"FFmpeg 解码帧数 {decoded_frame_count} 与 FFprobe 帧数 {probed_frame_count} 不一致",
            "frame_integrity_pass",
        )
    if expected_frame_count and decoded_frame_count != expected_frame_count:
        report.error(
            f"FFmpeg 解码帧数 {decoded_frame_count} 与 timeline.frame_count={expected_frame_count} 不一致",
            "frame_integrity_pass",
        )
    if large_difference_count:
        report.error(
            f"有 {large_difference_count} 个解码帧差超过 {max_difference}",
            "frame_integrity_pass",
            "transition_pass",
        )
    if freeze_run_count:
        report.error(
            f"发现 {freeze_run_count} 段至少 {freeze_limit} 帧的异常冻结",
            "frame_integrity_pass",
            "transition_pass",
        )
    unexpected_black_ratio = unexpected_black_count / decoded_frame_count if decoded_frame_count else 0.0
    if unexpected_black_count:
        message = (
            f"成片发现 {unexpected_black_count} 个非 iris_out 黑帧，"
            f"占比 {unexpected_black_ratio:.6f}"
        )
        if unexpected_black_ratio > black_max_ratio:
            report.error(message + f"，超过阈值 {black_max_ratio}", "frame_integrity_pass", "transition_pass")
        else:
            report.warning(message + f"，未超过阈值 {black_max_ratio}")

    subtitle_map = _active_subtitle_map(timeline, expected_frame_count) if expected_frame_count else []
    subtitle_active = any(bool(ids) for ids in subtitle_map)
    subtitle_checked = 0
    subtitle_missing = 0
    subtitle_low_contrast = 0
    min_subtitle_contrast: float | None = None
    band_decoded_count = 0
    subtitle_style = style.get("subtitle") if isinstance(style.get("subtitle"), dict) else {}
    min_contrast = _number(subtitle_style.get("min_contrast")) or 4.5
    min_background_fraction = _number(subtitle_style.get("min_background_fraction"))
    if min_background_fraction is None:
        min_background_fraction = 0.04
    contrast_tolerance = max(0.25, min_contrast * 0.10)
    band_height = _integer(subtitle_style.get("band_height")) or 160
    native_width = actual_width or expected_width
    native_height = actual_height or expected_height
    band_height = min(band_height, native_height)
    if subtitle_active and band_height > 0 and native_width > 0:
        try:
            band_frames = _decoded_rgb_frames(
                video_path,
                native_width,
                band_height,
                f"crop=iw:{band_height}:0:ih-{band_height}",
            )
            for raw in band_frames:
                index = band_decoded_count
                band_decoded_count += 1
                if index >= len(subtitle_map) or not subtitle_map[index]:
                    continue
                subtitle_checked += 1
                contrast, light_fraction, dark_count = _subtitle_band_check(raw, native_width, band_height)
                if contrast > 0:
                    min_subtitle_contrast = (
                        contrast if min_subtitle_contrast is None else min(min_subtitle_contrast, contrast)
                    )
                if light_fraction < min_background_fraction or dark_count < 16:
                    subtitle_missing += 1
                elif contrast < min_contrast - contrast_tolerance:
                    subtitle_low_contrast += 1
        except (RuntimeError, OSError, ValueError) as exc:
            report.error(f"原生字幕 band 解码失败: {exc}", "subtitle_pass")
        if decoded_frame_count and band_decoded_count != decoded_frame_count:
            report.error(
                f"字幕 band 解码帧数 {band_decoded_count} 与全帧解码数 {decoded_frame_count} 不一致",
                "subtitle_pass",
                "frame_integrity_pass",
            )
    if subtitle_missing:
        report.error(f"有 {subtitle_missing} 个字幕活动帧缺少可辨识字幕带/文字", "subtitle_pass")
    if subtitle_low_contrast:
        report.error(
            f"有 {subtitle_low_contrast} 个字幕活动帧对比度不足（容差后阈值 {min_contrast - contrast_tolerance:.3f}）",
            "subtitle_pass",
        )

    if not audio_streams:
        report.error("成片无音轨，无法检查音频偏移", "audio_sync_pass")
    elif not source_audio_path.is_file():
        report.error(f"源音频不存在或不是文件: {source_audio_path}", "audio_sync_pass")
    else:
        try:
            audio_offset_ms, audio_measurement = _measure_audio(source_audio_path, video_path, work_dir)
            max_audio_ms = _max_audio_ms(style)
            if _exceeds(abs(audio_offset_ms), max_audio_ms):
                report.error(
                    f"实际音频偏移 {audio_offset_ms:.3f}ms 超过 ±{max_audio_ms}ms（正值表示成片音频滞后）",
                    "audio_sync_pass",
                )
            confidence = _number(audio_measurement.get("confidence"))
            warning = audio_measurement.get("warning")
            if confidence is not None and confidence <= 0:
                report.error(
                    f"音频偏移无法可靠测量: {warning or '互相关置信度为 0'}",
                    "audio_sync_pass",
                )
            elif warning:
                report.warning(f"音频偏移测量警告: {warning}")
        except Exception as exc:
            report.error(f"音频偏移测量失败: {exc}", "audio_sync_pass")

    report.metrics.update(
        {
            "width": actual_width,
            "height": actual_height,
            "fps": actual_fps,
            "ffprobe_frame_count": probed_frame_count,
            "decoded_frame_count": decoded_frame_count,
            "band_decoded_frame_count": band_decoded_count,
            "max_timeline_error_ms": max_pts_error_ms,
            "pts_over_limit_count": pts_over_limit,
            "non_monotonic_pts_count": non_monotonic_pts,
            "min_mean_luma": min_luma,
            "max_mean_luma": max_luma,
            "max_frame_difference": max_decoded_difference,
            "black_frame_count": black_count,
            "allowed_iris_out_black_frames": allowed_iris_black_count,
            "unexpected_black_frame_count": unexpected_black_count,
            "unexpected_black_frame_ratio": unexpected_black_ratio,
            "freeze_run_count": freeze_run_count,
            "max_freeze_run_frames": max_freeze_run,
            "subtitle_active_frame_count": sum(bool(ids) for ids in subtitle_map),
            "subtitle_checked_frame_count": subtitle_checked,
            "subtitle_missing_frame_count": subtitle_missing,
            "subtitle_low_contrast_frame_count": subtitle_low_contrast,
            "min_subtitle_contrast": min_subtitle_contrast,
            "audio_offset_ms": audio_offset_ms,
            "audio_offset_confidence": _number(audio_measurement.get("confidence")),
            "audio_offset_method": audio_measurement.get("method"),
            "audio_offset_warning": audio_measurement.get("warning"),
            "audio_offset_positive_means": "rendered_audio_lag",
            "transition_check_scope": "duration_markers_black_frames_difference_and_freeze_only",
        }
    )
    result = report.result()
    result["audio_offset_ms"] = audio_offset_ms
    result["max_timeline_error_ms"] = max_pts_error_ms
    return result


def generate_contact_sheet(
    video_path: str | Path,
    output_path: str | Path,
    interval_seconds: float = 5,
) -> str:
    """按间隔抽取全片样本，FFmpeg tile 后用 Pillow 写入每张样本的时间戳。"""
    interval = _number(interval_seconds)
    if interval is None or interval <= 0:
        raise ValueError("interval_seconds 必须是正数")
    video_path = Path(video_path)
    output_path = Path(output_path)
    if not video_path.is_file():
        raise RuntimeError(f"成片不存在或不是文件: {video_path}")
    probe = _probe_media(video_path)
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        raise RuntimeError("无法生成联系表：输入没有视频流")
    duration = _number(video.get("duration"))
    if duration is None or duration <= 0:
        duration = _number(_pick(probe.get("format"), "duration"))
    if duration is None or duration <= 0:
        raise RuntimeError("无法生成联系表：视频时长无效或未知")

    sample_count = max(1, math.ceil(duration / interval - 1e-9))
    columns = max(1, math.ceil(math.sqrt(sample_count)))
    rows = max(1, math.ceil(sample_count / columns))
    tile_width, tile_height = 480, 270
    filter_graph = (
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval})',"
        f"scale={tile_width}:{tile_height}:flags=area,"
        f"tile={columns}x{rows}:padding=0:margin=0"
    )
    result = _run_external(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filter_graph,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        text=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or "未知 FFmpeg 错误"
        raise RuntimeError(f"FFmpeg 联系表抽帧失败: {detail}")
    if not result.stdout:
        raise RuntimeError("FFmpeg 联系表抽帧没有输出图像")

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("生成联系表需要 Pillow，但当前环境无法导入") from exc
    try:
        image = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    except Exception as exc:
        raise RuntimeError(f"无法读取 FFmpeg 联系表图像: {exc}") from exc
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    for index in range(sample_count):
        column = index % columns
        row = index // columns
        x = column * tile_width
        y = row * tile_height
        seconds = index * interval
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        timestamp = f"{hours:02d}:{minutes:02d}:{secs:02d}"
        try:
            bounds = draw.textbbox((0, 0), timestamp, font=font)
            text_width = bounds[2] - bounds[0]
            text_height = bounds[3] - bounds[1]
        except AttributeError:
            text_width, text_height = draw.textsize(timestamp, font=font)
        draw.rectangle((x + 4, y + 4, x + text_width + 14, y + text_height + 14), fill=(0, 0, 0))
        draw.text((x + 9, y + 7), timestamp, fill=(255, 255, 255), font=font)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
    except OSError as exc:
        raise RuntimeError(f"无法写入联系表 {output_path}: {exc}") from exc
    return str(output_path)


def run_qc(
    video_path: str | Path,
    source_audio_path: str | Path,
    timeline: dict[str, Any],
    style: dict[str, Any],
    frames_log_path: str | Path,
    work_dir: str | Path,
    qc_path: str | Path,
    correction: bool = False,
) -> dict[str, Any]:
    """运行全部 QC 并写出 qc_path；不会因内部检查通过而声称真人声同步已验证。"""
    timeline_result = validate_timeline(timeline, style)
    frames_result = validate_frames_log(frames_log_path, timeline, style)
    output_result = validate_output(
        video_path,
        source_audio_path,
        timeline,
        style,
        work_dir,
        correction=correction,
    )
    components = {
        "timeline": timeline_result,
        "frames_log": frames_result,
        "output": output_result,
    }
    errors: list[str] = []
    warnings: list[str] = []
    for name, component in components.items():
        errors.extend(f"{name}: {message}" for message in component["errors"])
        warnings.extend(f"{name}: {message}" for message in component["warnings"])
    gates = {
        flag: all(bool(component.get(flag, True)) for component in components.values())
        for flag in _REPORT_FLAGS
        if flag != "audio_sync_pass"
    }
    gates["audio_sync_pass"] = bool(output_result.get("audio_sync_pass"))
    timeline_errors = [
        _number(component.get("metrics", {}).get("max_timeline_error_ms")) or 0.0
        for component in components.values()
    ]
    max_timeline_error_ms = max(timeline_errors, default=0.0)
    alignment_status = timeline.get("alignment_status") if isinstance(timeline, dict) else None
    draft = alignment_status != "aligned"
    result: dict[str, Any] = {
        "passed": all(component.get("passed") for component in components.values()) and all(gates.values()),
        "draft": draft,
        **gates,
        "human_vocal_sync": "unverified",
        "max_timeline_error_ms": max_timeline_error_ms,
        "audio_offset_ms": output_result.get("audio_offset_ms"),
        "correction": bool(correction),
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "max_timeline_error_ms": max_timeline_error_ms,
            "audio_offset_ms": output_result.get("audio_offset_ms"),
            "correction": bool(correction),
            "timeline": timeline_result.get("metrics", {}),
            "frames_log": frames_result.get("metrics", {}),
            "output": output_result.get("metrics", {}),
        },
    }
    if not gates["timeline_sync_pass"] or not gates["audio_sync_pass"]:
        result["passed"] = False
    qc_path = Path(qc_path)
    try:
        qc_path.parent.mkdir(parents=True, exist_ok=True)
        qc_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法写入 QC 报告 {qc_path}: {exc}") from exc
    return result
