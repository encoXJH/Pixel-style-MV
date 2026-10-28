from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import art

__all__ = ["load_style", "plan_timeline", "render_video", "find_font"]

_SCENE_NAMES = (
    "campus",
    "bedroom",
    "rain_street",
    "seaside",
    "station",
    "train_interior",
    "stage",
    "highway",
    "ancient",
    "generic",
)
_BAYER_4 = np.array(
    [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]],
    dtype=np.float32,
)


def load_style(path: str | Path | None = None) -> dict:
    style_path = (
        Path(path).expanduser()
        if path is not None
        else Path(__file__).resolve().parent.parent / "assets" / "style.json"
    )
    with style_path.open("r", encoding="utf-8") as handle:
        style = json.load(handle)
    if not isinstance(style, dict):
        raise ValueError(f"样式文件必须是 JSON 对象：{style_path}")
    render = style.get("render")
    if not isinstance(render, dict):
        raise ValueError("样式文件缺少 render 配置")
    dimensions = {}
    for key in ("low_width", "low_height", "scale", "width", "height", "fps"):
        value = render.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"render.{key} 必须是数字")
        if int(value) != value or int(value) <= 0:
            raise ValueError(f"render.{key} 必须是正整数")
        dimensions[key] = int(value)
    if dimensions["width"] != dimensions["low_width"] * dimensions["scale"]:
        raise ValueError("输出宽度必须等于 low_width × scale")
    if dimensions["height"] != dimensions["low_height"] * dimensions["scale"]:
        raise ValueError("输出高度必须等于 low_height × scale")
    return style


def find_font(style: dict | None = None) -> str:
    candidates: list[Path] = []
    subtitle = (style or {}).get("subtitle", {})
    configured = subtitle.get("font_path") or subtitle.get("font")
    if configured:
        candidates.append(Path(str(configured)).expanduser())

    windows_root = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows")
    windows_fonts = windows_root / "Fonts"
    for name in (
        "simhei.ttf",
        "msyh.ttc",
        "msyhbd.ttc",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJKsc-Regular.otf",
        "NotoSansSC-Regular.ttf",
        "SourceHanSansSC-Regular.otf",
        "simsun.ttc",
    ):
        candidates.append(windows_fonts / name)

    for filename in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/NotoSansCJK-Regular.ttc",
    ):
        candidates.append(Path(filename))

    seen: set[str] = set()
    errors: list[str] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file():
            continue
        try:
            ImageFont.truetype(str(candidate), 24)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        return str(candidate.resolve())

    detail = f" 已配置字体读取失败：{'; '.join(errors)}" if errors else ""
    raise RuntimeError(
        "找不到可用的中文黑体字体。请安装微软雅黑（msyh.ttc）、黑体（simhei.ttf）"
        "或 Noto Sans CJK，也可在 style.json 的 subtitle.font_path 中指定字体文件。"
        + detail
    )


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数字")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数字")
    return result


def _alignment_lines(alignment: dict, duration: float) -> list[dict]:
    raw_lines = alignment.get("lines")
    if raw_lines is None:
        raw_lines = alignment.get("segments", alignment.get("subtitles", []))
    if isinstance(raw_lines, dict):
        raw_lines = list(raw_lines.values())
    if not isinstance(raw_lines, (list, tuple)):
        raise ValueError("alignment.lines 必须是数组")

    lines: list[dict] = []
    for index, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 条歌词对齐必须是对象")
        text = str(raw.get("text", "")).strip()
        start = max(0.0, min(duration, _number(raw.get("start"), f"lines[{index}].start")))
        end_value = raw.get("end", start)
        end = max(start, min(duration, _number(end_value, f"lines[{index}].end")))
        if not text or start >= duration:
            continue
        lines.append(
            {
                "id": str(raw.get("id", f"line_{index:04d}")),
                "text": text,
                "start": start,
                "end": end,
                "status": str(raw.get("status", alignment.get("status", "aligned"))),
            }
        )
    lines.sort(key=lambda item: item["start"])
    if not lines:
        raise ValueError("没有位于歌曲时长内的非空歌词")
    for index in range(len(lines) - 1):
        lines[index]["end"] = min(lines[index]["end"], lines[index + 1]["start"])
    return lines


def _build_lyric_cues(lines: list[dict], fps: int, frame_count: int) -> list[dict]:
    """每句歌词只量化一次；字幕、镜头和逐帧日志都引用这里的帧区间。"""
    cues: list[dict] = []
    previous_start = -1
    for index, line in enumerate(lines):
        target_start = float(line["start"])
        target_end = float(line["end"])
        if target_end <= target_start:
            raise ValueError(f"歌词 {line['id']} 缺少有效的起止时间")
        start_frame = min(frame_count, max(0, int(math.ceil(target_start * fps))))
        end_frame = min(frame_count, max(0, int(math.ceil(target_end * fps))))
        if start_frame <= previous_start:
            raise ValueError("相邻歌词起点落在同一视频帧，无法逐句绑定；请修正句级时间轴")
        if end_frame <= start_frame:
            raise ValueError(f"歌词 {line['id']} 短于一个视频帧，无法建立有效画面映射")
        cue_id = f"lyric_{index:04d}"
        cues.append(
            {
                "id": cue_id,
                "line_id": line["id"],
                "subtitle_id": f"subtitle_{index:04d}",
                "text": line["text"],
                "target_start": target_start,
                "target_end": target_end,
                "start": start_frame / fps,
                "end": end_frame / fps,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "error_start_ms": round((start_frame / fps - target_start) * 1000.0, 6),
                "error_end_ms": round((end_frame / fps - target_end) * 1000.0, 6),
                "shot_ids": [],
            }
        )
        previous_start = start_frame
    return cues


def _feature_times(features: dict, names: tuple[str, ...]) -> np.ndarray:
    value: Any = None
    for name in names:
        if name in features:
            value = features[name]
            break
    if value is None:
        return np.empty(0, dtype=np.float64)
    if isinstance(value, dict):
        value = value.get("times", value.get("time", value.get("values", [])))
    if not isinstance(value, (list, tuple, np.ndarray)):
        return np.empty(0, dtype=np.float64)
    if len(value) > 0 and isinstance(value[0], dict):
        value = [item.get("time", item.get("seconds")) for item in value]
    try:
        array = np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return np.empty(0, dtype=np.float64)
    return array[np.isfinite(array)]


def _energy_curve(features: dict, duration: float) -> tuple[np.ndarray, np.ndarray]:
    raw = features.get("rms", features.get("energy", features.get("energies", [])))
    times: Any = None
    if isinstance(raw, dict):
        times = raw.get("times", raw.get("time"))
        raw = raw.get("values", raw.get("rms", raw.get("energy", [])))
    if not isinstance(raw, (list, tuple, np.ndarray)) or len(raw) == 0:
        return np.empty(0), np.empty(0)
    if isinstance(raw[0], dict):
        times = [item.get("time", item.get("seconds")) for item in raw]
        raw = [item.get("rms", item.get("energy", item.get("value"))) for item in raw]
    try:
        values = np.asarray(raw, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return np.empty(0), np.empty(0)
    finite = np.isfinite(values)
    if not finite.any():
        return np.empty(0), np.empty(0)
    values = np.where(finite, values, 0.0)
    values = np.maximum(values, 0.0)
    if times is None:
        for key in ("rms_times", "energy_times", "frame_times", "times"):
            if key in features:
                times = features[key]
                break
    if times is not None:
        try:
            time_array = np.asarray(times, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError):
            time_array = np.empty(0)
        if len(time_array) != len(values) or not np.isfinite(time_array).all():
            time_array = np.linspace(0.0, duration, len(values))
    else:
        time_array = np.linspace(0.0, duration, len(values))
    order = np.argsort(time_array)
    time_array = time_array[order]
    values = values[order]
    scale = float(np.percentile(values, 95))
    if scale <= 1e-12:
        return time_array, np.zeros_like(values)
    return time_array, np.clip(values / scale, 0.0, 1.0)


def _energy_at(curve: tuple[np.ndarray, np.ndarray], seconds: float) -> float:
    times, values = curve
    if not len(values):
        return 0.0
    return float(np.interp(seconds, times, values))


def _keyword_score(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    score = 0
    for keyword in keywords:
        key = str(keyword).lower().strip()
        if key:
            score += lowered.count(key)
    return score


def _match_scene(text: str, style: dict) -> str:
    scenes = style.get("scenes", {})
    ranked = []
    for index, name in enumerate(_SCENE_NAMES):
        if name == "generic":
            continue
        score = _keyword_score(text, scenes.get(name, {}).get("keywords", []))
        ranked.append((score, -index, name))
    best = max(ranked, default=(0, 0, "generic"))
    return best[2] if best[0] > 0 else "generic"


def _select_props(text: str, scene: str, motif: str, style: dict) -> list[str]:
    definitions = style.get("prop_keywords", {})
    ranked = []
    for index, (name, keywords) in enumerate(definitions.items()):
        score = _keyword_score(text, keywords)
        if score:
            ranked.append((-score, index, name))
    ranked.sort()
    result: list[str] = []
    for _, _, name in ranked:
        if name not in result:
            result.append(name)
    return result[:2]


def _global_motif(lines: list[dict], style: dict) -> str:
    text = "\n".join(line["text"] for line in lines)
    ranked = []
    for index, (name, keywords) in enumerate(style.get("prop_keywords", {}).items()):
        score = _keyword_score(text, keywords)
        if score:
            ranked.append((-score, index, name))
    if ranked:
        ranked.sort()
        return ranked[0][2]
    first_scene = _match_scene(lines[0]["text"], style)
    props = style.get("scenes", {}).get(first_scene, {}).get("props", [])
    return props[0] if props else "letter"


_STORY_ARCS = {
    "station": ("station", "train_interior", "seaside", "station", "bedroom", "rain_street", "seaside", "station"),
    "seaside": ("seaside", "train_interior", "station", "seaside", "bedroom", "rain_street", "seaside", "seaside"),
    "campus": ("campus", "campus", "train_interior", "campus", "bedroom", "rain_street", "campus", "campus"),
    "bedroom": ("bedroom", "train_interior", "station", "seaside", "bedroom", "rain_street", "bedroom", "station"),
    "rain_street": ("rain_street", "train_interior", "station", "bedroom", "rain_street", "seaside", "station", "rain_street"),
    "stage": ("stage", "bedroom", "stage", "train_interior", "bedroom", "rain_street", "stage", "stage"),
    "highway": ("highway", "train_interior", "seaside", "highway", "bedroom", "rain_street", "highway", "seaside"),
    "ancient": ("ancient", "ancient", "ancient", "ancient", "bedroom", "ancient", "ancient", "ancient"),
    "generic": ("generic", "train_interior", "seaside", "generic", "bedroom", "rain_street", "seaside", "station"),
}

_PHASE_SHOTS = {
    "prologue": ("landscape",),
    "encounter": ("wide", "medium", "object", "wide", "close", "medium"),
    "development": ("wide", "medium", "close", "wide", "object", "medium"),
    "separation": ("wide", "close", "medium", "object", "wide", "close"),
    "memory": ("object", "close", "wide", "medium", "object", "close"),
    "return": ("wide", "medium", "wide", "close", "medium", "wide"),
    "epilogue": ("landscape",),
}

# 只有明确的地点或行动意象才能覆盖章节场景；梦、你、夜等词不能单独跳场景。
_SEMANTIC_SCENE_KEYWORDS = {
    "train_interior": ("车厢", "车窗", "上车", "carriage", "train interior"),
    "station": ("车站", "站台", "列车", "火车", "station", "platform", "train"),
    "bedroom": ("房间", "卧室", "床", "窗下", "回家", "失眠", "room", "bedroom"),
    "rain_street": ("雨里", "雨中", "下雨", "雨夜", "街道", "街头", "rain", "street"),
    "highway": ("公路", "这条路", "路的两端", "路的中间", "拉斯维加斯", "沙漠", "无人烟", "往返", "highway", "road"),
    "seaside": ("海", "沙滩", "海岸", "日落", "黄昏", "晚霞", "夕阳", "金黄", "sea", "beach", "sunset"),
    "campus": ("学校", "校园", "教室", "同桌", "毕业", "课桌", "校服", "campus", "classroom"),
    "stage": ("舞台", "掌声", "演唱会", "麦克风", "stage", "concert"),
    "ancient": ("江湖", "剑客", "酒馆", "天涯", "故人", "sword", "jianghu"),
}


def _story_theme(lines: list[dict], style: dict) -> str:
    if len(lines) <= 3:
        for line in lines:
            matched = _match_scene(line["text"], style)
            if matched != "generic":
                return matched
    joined = "\n".join(line["text"] for line in lines)
    if _keyword_score(joined, style.get("scenes", {}).get("seaside", {}).get("keywords", [])) and _keyword_score(
        joined, ["离开", "回来", "等待", "未来", "车站", "站台", "列车", "远方"]
    ):
        return "station"
    ranked = []
    for index, name in enumerate(_SCENE_NAMES):
        if name in ("generic", "train_interior"):
            continue
        score = _keyword_score(joined, style.get("scenes", {}).get(name, {}).get("keywords", []))
        ranked.append((score, -index, name))
    best = max(ranked, default=(0, 0, "generic"))
    return best[2] if best[0] > 0 else "generic"


def _semantic_scene(text: str) -> str:
    ranked = []
    for priority, (scene, keywords) in enumerate(_SEMANTIC_SCENE_KEYWORDS.items()):
        score = _keyword_score(text, list(keywords))
        if score:
            ranked.append((-score, priority, scene))
    return min(ranked)[2] if ranked else "generic"


def _story_scene(theme: str, progress: float) -> str:
    arc = _STORY_ARCS.get(theme, _STORY_ARCS["generic"])
    chapter = min(len(arc) - 1, int(max(0.0, min(0.999999, progress)) * len(arc)))
    return arc[chapter]


def _story_phase(progress: float, opening: bool = False, closing: bool = False) -> str:
    if opening:
        return "prologue"
    if closing:
        return "epilogue"
    if progress < 0.18:
        return "encounter"
    if progress < 0.42:
        return "development"
    if progress < 0.66:
        return "separation"
    if progress < 0.84:
        return "memory"
    return "return"


def _shot_weather(theme: str, scene: str, phase: str, text: str, progress: float) -> str:
    if _keyword_score(text, ["雪", "初雪", "snow"]):
        return "snow"
    if scene == "rain_street" or _keyword_score(text, ["雨", "下雨", "rain"]):
        return "rain"
    if _keyword_score(text, ["多云", "阴天", "阴云", "云层", "cloudy", "overcast"]):
        return "cloudy"
    if _keyword_score(text, ["日落", "黄昏", "晚霞", "夕阳", "金黄", "sunset", "dusk"]):
        return "sunset"
    if _keyword_score(text, ["晴天", "放晴", "阳光", "sunny", "clear sky"]):
        return "day"
    if _keyword_score(text, ["夜", "月亮", "星空", "失眠", "night", "moon"]):
        return "night"
    if theme == "station" and 0.72 <= progress < 0.82:
        return "snow"
    if phase == "separation" and scene in ("station", "rain_street"):
        return "rain"
    if scene == "seaside" or phase in ("memory", "return") or _keyword_score(
        text, ["夕阳", "日落", "黄昏", "晚霞", "sunset"]
    ):
        return "sunset"
    if scene == "bedroom" and phase not in ("return", "epilogue"):
        return "night"
    return "day"


def _camera_plan(shot_type: str, phase: str, index: int, style: dict) -> dict:
    config = style.get("camera", {})
    kind = str(config.get("default_by_shot", {}).get(shot_type, "static")) if config.get("enabled", True) else "static"
    if phase == "memory" and shot_type in ("wide", "medium"):
        kind = "pull_out"
    elif phase == "separation" and shot_type == "close":
        kind = "push_in"
    elif shot_type == "wide" and index % 2:
        kind = "pan_left"
    return {
        "kind": kind,
        "max_crop_pixels": max(0, int(config.get("max_crop_pixels", 12))),
        "pan_pixels": max(0, int(config.get("pan_pixels", 8))),
        "hold_frames": max(1, int(config.get("motion_hold_frames", 2))),
        "direction": -1 if kind == "pan_left" else 1,
    }


def _shot_effects(base: dict, progress: float) -> list[str]:
    text = base["text"]
    effects: list[str] = []
    if base["story_phase"] == "prologue":
        effects.append("title_card")
    if base["story_phase"] == "memory" or _keyword_score(text, ["照片", "相片", "回忆", "记忆", "记得", "photo", "memory", "remember"]):
        effects.append("photo_frame" if base["shot_type"] in ("object", "close") else "film_frame")
    if _keyword_score(text, ["梦", "想象", "如果", "幻想", "dream", "imagine"]):
        effects.append("thought_bubble")
    if _keyword_score(text, ["烟花", "烟火", "firework"]):
        effects.append("fireworks")
    if base["action"] in ("embrace", "heart_glass") or _keyword_score(text, ["爱", "心动", "喜欢", "love", "heart"]):
        effects.append("pixel_hearts")
    if base["scene"] == "stage" and progress >= 0.4:
        effects.append("stage_lights")
    return list(dict.fromkeys(effects))[:3]


def _shot_sfx(base: dict) -> list[str]:
    mapping = {
        "walking": "footsteps",
        "cycling": "bicycle_bell",
        "reading": "paper",
        "photographing": "camera_shutter",
        "playing": "room_tone",
        "train": "train",
        "heart_glass": "paper",
    }
    cues = [mapping[base["action"]]] if base["action"] in mapping else []
    if base["weather"] == "rain":
        cues.append("rain")
    elif base["weather"] == "snow":
        cues.append("wind")
    if base["scene"] == "seaside":
        cues.append("waves")
    if "fireworks" in base.get("effects", []):
        cues.append("fireworks")
    return list(dict.fromkeys(cues))


def _montage_variant(
    shot: dict,
    base: dict,
    chunk_index: int,
    theme: str,
    motif: str,
    pair_story: bool,
    style: dict,
) -> dict:
    """把长镜头拆成有叙事变化的蒙太奇，而不是复制同一构图。"""
    kind = base["kind"]
    shot["kind"] = kind
    shot["variant"] = int(chunk_index)
    if kind == "lyric":
        return shot
    if kind == "prologue":
        if chunk_index == 0:
            shot.update(
                action="title_board",
                shot_type="landscape",
                characters=[],
                props=[],
                compare_weather=None,
                effects=[],
                sfx=[],
            )
            shot["camera"] = {"kind": "static", "max_crop_pixels": 0, "pan_pixels": 0, "hold_frames": 2, "direction": 1}
            shot["blocking"] = {"layout": "environment_only", "primary_action": "title_board", "screen_direction": "left_to_right"}
            shot["density"] = {"background_layers": 5, "primary_actions": 1, "object_props": 0, "emotional_overlays": 0}
        else:
            shot["effects"] = []
            shot["shot_type"] = "landscape" if chunk_index % 2 else "wide"
        return shot

    arc = _STORY_ARCS.get(theme, _STORY_ARCS["generic"])
    scene = base["scene"] if chunk_index == 0 else arc[(chunk_index * 3 + 1) % len(arc)]
    shot_types = ("wide", "medium", "close", "object", "landscape", "wide", "medium", "close")
    shot_type = shot_types[(chunk_index - 1) % len(shot_types)] if chunk_index else base["shot_type"]
    weather = _shot_weather(theme, scene, base["story_phase"], base["text"], 0.8)
    characters: list[str] = []
    if shot_type not in ("landscape", "object"):
        characters = ["protagonist"]
        if pair_story and (shot_type == "close" or chunk_index % 3 != 1):
            characters.append("companion")
    props = _select_props(base["text"], scene, motif, style)
    if shot_type == "landscape":
        props = ["cat"] if scene == "station" else []
    action = "waiting"
    if shot_type == "object" and chunk_index % 2:
        action = "heart_glass"
        props = []
    elif scene == "station" and chunk_index % 3 == 0:
        action = "train"
    elif characters and scene in ("seaside", "highway") and shot_type != "close" and chunk_index % 2:
        action = "walking"
    if shot_type == "object" and action != "heart_glass":
        props = (props or [motif])[:2]

    shot.update(
        scene=scene,
        shot_type=shot_type,
        weather=weather,
        compare_weather=None,
        characters=characters,
        props=props,
        action=action,
        expression=base.get("expression", "neutral"),
        line_id=None,
        lyric_cue_id=None,
        boundary_source=kind,
        effects=[],
    )
    scene_config = style.get("scenes", {}).get(scene, {})
    shot["palette"] = list(scene_config.get("palette", style.get("scenes", {}).get("generic", {}).get("palette", [])))
    shot["filter"] = scene_config.get("filter", "default")
    if weather in ("rain", "night"):
        shot["filter"] = "rain_night"
    elif weather in ("sunset", "snow"):
        shot["filter"] = "dawn"
    if scene == "station" and shot_type in ("wide", "medium") and chunk_index % 4 == 2:
        shot["weather"] = "day"
        shot["compare_weather"] = "snow"
        shot["filter"] = "default"
    shot["camera"] = _camera_plan(shot_type, base["story_phase"], chunk_index, style)
    if shot["compare_weather"]:
        shot["camera"]["kind"] = "static"
    if shot_type == "object":
        layout = "object_table"
    elif not characters:
        layout = "environment_only"
    elif shot_type == "close" and len(characters) > 1:
        layout = "dual_portrait"
    elif len(characters) > 1 and scene in ("station", "train_interior"):
        layout = "bench_pair"
    elif len(characters) > 1 and base["story_phase"] == "separation":
        layout = "separate_sides"
    else:
        layout = "two_shot" if len(characters) > 1 else "center_single"
    shot["blocking"] = {
        "layout": layout,
        "primary_action": action,
        "screen_direction": "left_to_right" if chunk_index % 2 == 0 else "right_to_left",
    }
    if base["story_phase"] == "memory" and shot_type in ("object", "close"):
        shot["effects"] = ["photo_frame"]
    if base.get("energy_peak") and scene in ("seaside", "station", "stage"):
        shot["effects"] = list(dict.fromkeys(shot["effects"] + ["fireworks"]))
    shot["sfx"] = _shot_sfx(shot)
    shot["density"] = {
        "background_layers": int(style.get("density", {}).get("background_layers", 5)),
        "primary_actions": 1,
        "object_props": len(props) if shot_type == "object" else 0,
        "emotional_overlays": min(2, len(shot["effects"])),
    }
    return shot


def plan_timeline(alignment: dict, features: dict, style: dict) -> dict:
    if not isinstance(alignment, dict) or not isinstance(features, dict):
        raise ValueError("alignment 和 features 必须是 JSON 对象")
    render = style["render"]
    timing = style.get("timing", {})
    fps = int(render["fps"])
    duration_value = alignment.get(
        "duration",
        alignment.get("song_duration", alignment.get("audio_duration", features.get("duration"))),
    )
    raw_lines = alignment.get("lines", alignment.get("segments", alignment.get("subtitles", [])))
    if duration_value is None and isinstance(raw_lines, list) and raw_lines:
        duration_value = max(_number(line.get("end", 0), "line.end") for line in raw_lines)
    duration = _number(duration_value, "duration")
    if duration <= 0:
        raise ValueError("duration 必须大于 0")
    lines = _alignment_lines(alignment, duration)
    frame_count = int(math.ceil(duration * fps))
    if frame_count <= 0:
        raise ValueError("时长不足以生成视频帧")
    lyric_cues = _build_lyric_cues(lines, fps, frame_count)
    cue_by_line = {cue["line_id"]: cue for cue in lyric_cues}

    min_shot = max(0.0, float(timing.get("min_shot_seconds", 0.6)))
    max_shot = max(1.0 / fps, float(timing.get("max_shot_seconds", 8.0)))
    max_shot_frames = max(1, int(math.floor(max_shot * fps)))
    motif = _global_motif(lines, style)
    curve = _energy_curve(features, duration)

    bases: list[dict] = []
    first = lines[0]
    if first["start"] > 0:
        bases.append(
            {
                "kind": "prologue",
                "line_id": None,
                "text": first["text"],
                "target_start": 0.0,
                "target_end": first["start"],
                "opening": True,
                "closing": False,
            }
        )
    # 歌词、句间空白和尾奏分开规划，避免把上一句的视觉 line_id 延续到间奏。
    for index, line in enumerate(lines):
        bases.append(
            {
                "kind": "lyric",
                "line_id": line["id"],
                "text": line["text"],
                "target_start": line["start"],
                "target_end": line["end"],
                "opening": False,
                "closing": False,
            }
        )
        next_start = lines[index + 1]["start"] if index + 1 < len(lines) else duration
        if next_start <= line["end"]:
            continue
        closing = index + 1 == len(lines)
        gap_text = first["text"] if closing else f"{line['text']}\n{lines[index + 1]['text']}"
        bases.append(
            {
                "kind": "epilogue" if closing else "instrumental",
                "line_id": None,
                "text": gap_text,
                "target_start": line["end"],
                "target_end": next_start,
                "opening": False,
                "closing": closing,
            }
        )
    if len(bases) > frame_count:
        raise ValueError("歌词句数超过可用视频帧数，无法在不合并短句的情况下连续排布")

    theme = _story_theme(lines, style)
    joined_lyrics = "\n".join(line["text"] for line in lines)
    pair_story = bool(_keyword_score(joined_lyrics, ["你", "你们", "我们", "同桌", "故人", "friend", "together", "you", "us"]))
    phase_counts: dict[str, int] = {}
    song_title = str(features.get("title") or alignment.get("title") or "像素 · 纪行")
    artist = str(features.get("artist") or alignment.get("artist") or "")
    for base_index, base in enumerate(bases):
        kind = base["kind"]
        opening = bool(base["opening"])
        closing = bool(base["closing"])
        progress = max(0.0, min(1.0, float(base["target_start"]) / duration))
        semantic = _semantic_scene(base["text"]) if kind == "lyric" else "generic"
        base["scene"] = theme if opening or closing else (semantic if semantic != "generic" else _story_scene(theme, progress))
        base["story_phase"] = _story_phase(progress, opening, closing)
        base["chapter"] = min(8, int(progress * 8) + 1)
        base["variant"] = 0
        phase_pattern = _PHASE_SHOTS[base["story_phase"]]
        pattern_index = phase_counts.get(base["story_phase"], 0)
        phase_counts[base["story_phase"]] = pattern_index + 1
        base["shot_type"] = phase_pattern[pattern_index % len(phase_pattern)]
        if kind == "instrumental":
            montage_shots = ("wide", "medium", "close", "object", "landscape", "wide")
            base["shot_type"] = montage_shots[pattern_index % len(montage_shots)]
        if _keyword_score(base["text"], ["相拥", "拥抱", "抱紧", "靠近"]):
            base["shot_type"] = "medium"
        elif kind == "lyric" and _keyword_score(base["text"], ["画心", "一颗心", "表白", "雾气", "玻璃"]):
            base["shot_type"] = "object"
        elif _keyword_score(base["text"], ["单车", "自行车", "骑行", "阅读", "看书", "课本", "拍照", "相机", "快门", "bicycle", "reading", "photograph", "camera"]):
            base["shot_type"] = "medium"
        base["weather"] = _shot_weather(theme, base["scene"], base["story_phase"], base["text"], progress)
        if closing:
            base["weather"] = bases[0].get("weather", "day")
        base["compare_weather"] = None
        if theme == "station" and (opening or closing):
            base["weather"] = "day" if opening else bases[0].get("weather", "day")
            base["compare_weather"] = "rain" if opening else "snow"
        elif (
            pair_story
            and kind == "lyric"
            and base["scene"] == "station"
            and base["story_phase"] == "return"
            and base["shot_type"] in ("wide", "medium")
        ):
            base["weather"] = "day"
            base["compare_weather"] = "snow"
        scene_config = style.get("scenes", {}).get(base["scene"], {})
        base["palette"] = list(scene_config.get("palette", style.get("scenes", {}).get("generic", {}).get("palette", [])))
        base["props"] = _select_props(base["text"], base["scene"], motif, style)
        base["filter"] = scene_config.get("filter", "default")
        if base["compare_weather"]:
            base["filter"] = "default"
        elif base["scene"] == "station" and base["weather"] in ("rain", "night"):
            base["filter"] = "station_rain"
        elif base["weather"] in ("rain", "night"):
            base["filter"] = "rain_night"
        elif base["weather"] in ("sunset", "snow"):
            base["filter"] = "dawn"
        base["energy"] = _energy_at(curve, (base["target_start"] + base["target_end"]) / 2)
        embrace = kind == "lyric" and pair_story and bool(_keyword_score(base["text"], ["相拥", "拥抱", "抱紧", "靠近"]))
        heart_glass = base["shot_type"] == "object" and kind == "lyric" and bool(
            _keyword_score(base["text"], ["画心", "一颗心", "表白", "雾气", "玻璃"])
        )
        dual_portrait = kind == "lyric" and pair_story and base["shot_type"] == "close" and not embrace
        characters: list[str] = []
        if base["shot_type"] not in ("landscape", "object"):
            if embrace or dual_portrait:
                characters = ["protagonist", "companion"]
            elif base["shot_type"] == "close":
                characters = ["companion" if pair_story and base_index % 2 else "protagonist"]
            else:
                characters = ["protagonist"]
                if pair_story and base_index % 3 != 1:
                    characters.append("companion")
        base["characters"] = characters
        if base["shot_type"] == "landscape":
            base["props"] = ["cat"] if closing else []
        elif base["shot_type"] == "object":
            object_props = [name for name in base["props"] if name != "cat"][:2]
            base["props"] = [] if heart_glass else (object_props or [motif])
        base["expression"] = "sad" if _keyword_score(base["text"], ["哭", "泪", "离开", "告别", "黑暗", "孤独", "迷失", "sad", "tears", "goodbye"]) else (
            "happy" if _keyword_score(base["text"], ["相拥", "拥抱", "爱", "喜欢", "耀眼", "烟花", "happy", "love"]) else "neutral"
        )
        base["action"] = "waiting"
        if heart_glass:
            base["action"] = "heart_glass"
        elif embrace:
            base["action"] = "embrace"
        elif base["scene"] == "station" and (opening or _keyword_score(base["text"], ["列车", "火车", "train"])):
            base["action"] = "train"
        elif characters and _keyword_score(base["text"], ["单车", "自行车", "骑行", "bicycle", "bike", "cycling"]):
            base["action"] = "cycling"
        elif characters and _keyword_score(base["text"], ["阅读", "看书", "课本", "reading", "book"]):
            base["action"] = "reading"
        elif characters and _keyword_score(base["text"], ["拍照", "相机", "快门", "photograph", "camera"]):
            base["action"] = "photographing"
        elif "guitar" in base["props"] and base["scene"] in ("stage", "bedroom"):
            base["action"] = "playing"
        elif characters and _keyword_score(base["text"], ["寻找", "迷失", "奔跑", "走过", "行走", "离开", "告别", "赶来", "往返", "walking", "running"]):
            base["action"] = "walking"
        elif len(characters) > 1 and _keyword_score(base["text"], ["交谈", "说话", "聊", "唱着", "talk", "sing"]):
            base["action"] = "talking"
        elif kind == "instrumental" and characters and base["scene"] in ("seaside", "highway"):
            base["action"] = "walking"
        base["camera"] = _camera_plan(base["shot_type"], base["story_phase"], base_index, style)
        if base["compare_weather"]:
            base["camera"]["kind"] = "static"
        if base["shot_type"] == "object":
            layout = "object_table"
        elif not characters:
            layout = "environment_only"
        elif dual_portrait:
            layout = "dual_portrait"
        elif embrace:
            layout = "close_pair"
        elif len(characters) > 1 and base["scene"] in ("station", "train_interior") and base["action"] in ("waiting", "talking"):
            layout = "bench_pair"
        elif len(characters) > 1 and base["story_phase"] == "separation":
            layout = "separate_sides"
        else:
            layout = "two_shot" if len(characters) > 1 else "center_single"
        base["blocking"] = {
            "layout": layout,
            "primary_action": base["action"],
            "screen_direction": "left_to_right" if base_index % 2 == 0 else "right_to_left",
        }
        base["effects"] = _shot_effects(base, progress)
        base["sfx"] = _shot_sfx(base)
        base["title"] = song_title
        base["artist"] = artist
        base["density"] = {
            "background_layers": int(style.get("density", {}).get("background_layers", 5)),
            "primary_actions": 1,
            "object_props": len(base["props"]) if base["shot_type"] == "object" else 0,
            "emotional_overlays": min(2, len(base["effects"])),
        }
    eligible_energy = [base["energy"] for base in bases if not base["opening"] and not base["closing"]]
    if eligible_energy and max(eligible_energy) >= 0.75 and max(eligible_energy) > min(eligible_energy):
        climax = max(
            (base for base in bases if not base["opening"] and not base["closing"]),
            key=lambda base: base["energy"],
        )
        climax["energy_peak"] = True
        if climax["scene"] in ("seaside", "stage", "station") and "fireworks" not in climax["effects"]:
            climax["effects"].append("fireworks")
            climax["sfx"].append("fireworks")

    # 句界镜头与字幕必须使用同一个 cue 帧，不允许再独立取整或吸附节拍。
    desired_frames = []
    for base in bases:
        if base["line_id"] is not None:
            desired_frames.append(int(cue_by_line[base["line_id"]]["start_frame"]))
        else:
            desired_frames.append(min(frame_count, int(math.ceil(float(base["target_start"]) * fps))))
    desired_frames.append(frame_count)

    for index in range(1, len(desired_frames) - 1):
        desired_frames[index] = max(desired_frames[index], desired_frames[index - 1] + 1)
    for index in range(len(desired_frames) - 2, 0, -1):
        desired_frames[index] = min(desired_frames[index], desired_frames[index + 1] - 1)

    shots: list[dict] = []
    base_indices: list[int] = []
    energies: list[float] = []
    top_seed = int(style.get("seed", 0))
    for base_index, base in enumerate(bases):
        first_frame = desired_frames[base_index]
        last_frame = desired_frames[base_index + 1]
        for chunk_start in range(first_frame, last_frame, max_shot_frames):
            chunk_end = min(last_frame, chunk_start + max_shot_frames)
            shot_index = len(shots)
            target_start = base["target_start"] if chunk_start == first_frame else chunk_start / fps
            lyric_cue_id = cue_by_line[base["line_id"]]["id"] if base["line_id"] is not None else None
            shot = {
                "id": f"shot_{shot_index:04d}",
                "line_id": base["line_id"],
                "lyric_cue_id": lyric_cue_id,
                "boundary_source": lyric_cue_id or base["kind"],
                "target_start": float(target_start),
                "start": chunk_start / fps,
                "end": chunk_end / fps,
                "start_frame": int(chunk_start),
                "end_frame": int(chunk_end),
                "scene": base["scene"],
                "palette": list(base["palette"]),
                "props": list(base["props"]),
                "characters": list(base["characters"]),
                "filter": base["filter"],
                "art_version": 3,
                "shot_type": base["shot_type"] if chunk_start == first_frame else ("medium" if base["shot_type"] == "wide" else base["shot_type"]),
                "weather": base["weather"],
                "compare_weather": base["compare_weather"],
                "story_phase": base["story_phase"],
                "chapter": base["chapter"],
                "narrative_beat": base["kind"],
                "action": base["action"],
                "camera": dict(base["camera"]),
                "blocking": dict(base["blocking"]),
                "effects": list(base["effects"]),
                "sfx": list(dict.fromkeys(base["sfx"])),
                "density": dict(base["density"]),
                "title": base["title"],
                "artist": base["artist"],
                "expression": base["expression"],
                "energy": _energy_at(curve, (chunk_start + chunk_end) / (2 * fps)),
                "energy_peak": bool(base.get("energy_peak", False)) and chunk_start == first_frame,
                "pose_hold_frames": int(style.get("dynamics", {}).get("pose_hold_frames", 6)),
                "transition_in": "hard_cut",
                "transition_frames": 0,
                "seed": top_seed + shot_index * 7919,
                "beat_snap_ms": 0.0,
                "beat_snap_applied": False,
                "weather_from": None,
                "weather_transition_frames": 0,
                "weather_transition_steps": max(2, int(style.get("dynamics", {}).get("weather_transition_steps", 5))),
            }
            chunk_index = (chunk_start - first_frame) // max_shot_frames
            shots.append(_montage_variant(shot, base, chunk_index, theme, motif, pair_story, style))
            if lyric_cue_id is not None:
                cue_by_line[base["line_id"]]["shot_ids"].append(f"shot_{shot_index:04d}")
            base_indices.append(base_index)
            energies.append(float(shots[-1]["energy"]))

    if len(shots) > 1 and bases[0]["kind"] == "prologue":
        first_shot = shots[0]
        last_shot = shots[-1]
        last_shot.update(
            scene=first_shot["scene"],
            palette=list(first_shot["palette"]),
            weather=first_shot["weather"],
            compare_weather=None,
            shot_type="landscape",
            action="title_board",
            characters=[],
            props=[],
            effects=[],
            sfx=[],
            expression="neutral",
            title=first_shot["title"],
            artist=first_shot.get("artist", ""),
            filter="default",
        )
        last_shot["camera"] = {"kind": "static", "max_crop_pixels": 0, "pan_pixels": 0, "hold_frames": 2, "direction": 1}
        last_shot["blocking"] = {"layout": "environment_only", "primary_action": "title_board", "screen_direction": "left_to_right"}
        last_shot["density"] = {"background_layers": 5, "primary_actions": 1, "object_props": 0, "emotional_overlays": 0}

    weather_frames = max(0, int(style.get("dynamics", {}).get("weather_transition_frames", 15)))
    for index in range(1, len(shots)):
        shot = shots[index]
        previous_weather = str(shots[index - 1].get("weather", "day"))
        if previous_weather == shot["weather"] or shot.get("compare_weather"):
            continue
        shot["weather_from"] = previous_weather
        shot["weather_transition_frames"] = min(
            weather_frames,
            max(0, int(shot["end_frame"]) - int(shot["start_frame"]) - 1),
        )

    transition_style = style.get("transitions", {})

    def transition_duration(name: str) -> int:
        return max(0, int(transition_style.get(name, {}).get("duration_frames", 0)))

    def set_transition(index: int, name: str) -> None:
        frames = min(transition_duration(name), max(0, shots[index]["end_frame"] - shots[index]["start_frame"] - 1))
        shots[index]["transition_in"] = name
        shots[index]["transition_frames"] = frames

    if shots and transition_style.get("iris_out", {}).get("ending_only", True):
        last_shot = shots[-1]
        last_shot["transition_out"] = "iris_out"
        last_shot["transition_out_frames"] = min(
            transition_duration("iris_out"),
            max(0, frame_count - 1),
        )

    white_limit = max(0, int(transition_style.get("white_flash", {}).get("max_per_video", 2)))
    white_candidates = sorted(
        (
            index
            for index in range(1, len(shots))
            if energies[index] >= 0.75
            and base_indices[index] != base_indices[index - 1]
            and shots[index]["transition_in"] == "hard_cut"
        ),
        key=lambda index: (-energies[index], index),
    )
    for index in white_candidates[:white_limit]:
        set_transition(index, "white_flash")

    if len(shots) >= 5 and int(transition_style.get("scroll_wipe", {}).get("max_per_video", 1)) > 0:
        scroll_candidates = [
            index
            for index in range(1, len(shots))
            if shots[index]["transition_in"] == "hard_cut"
            and base_indices[index] != base_indices[index - 1]
            and shots[index]["scene"] != shots[index - 1]["scene"]
        ]
        if scroll_candidates:
            index = min(scroll_candidates, key=lambda item: (abs(shots[item]["start"] / duration - 0.62), item))
            set_transition(index, "scroll_wipe")

    def place_reference_transition(name: str, candidates: list[int]) -> None:
        limit = max(0, int(transition_style.get(name, {}).get("max_per_video", 0)))
        placed = 0
        for index in candidates:
            if placed >= limit:
                break
            current = shots[index]["transition_in"]
            if current == "hard_cut" or (name in ("photo_reveal", "memory_dissolve") and current in ("white_flash", "scroll_wipe")):
                set_transition(index, name)
                placed += 1

    memory_candidates = [
        index for index in range(1, len(shots))
        if shots[index]["story_phase"] == "memory" and base_indices[index] != base_indices[index - 1]
    ]
    place_reference_transition(
        "photo_reveal",
        [index for index in memory_candidates if shots[index]["shot_type"] == "object"],
    )
    place_reference_transition("memory_dissolve", memory_candidates)
    place_reference_transition(
        "match_cut",
        [
            index for index in range(1, len(shots))
            if shots[index]["scene"] == shots[index - 1]["scene"]
            and shots[index]["shot_type"] != shots[index - 1]["shot_type"]
            and base_indices[index] != base_indices[index - 1]
        ],
    )
    place_reference_transition(
        "film_wipe",
        [
            index for index in range(1, len(shots))
            if shots[index]["chapter"] != shots[index - 1]["chapter"]
            and base_indices[index] != base_indices[index - 1]
        ],
    )

    subtitles = [
        {
            "id": cue["subtitle_id"],
            "cue_id": cue["id"],
            "line_id": cue["line_id"],
            "text": cue["text"],
            "target_start": cue["target_start"],
            "target_end": cue["target_end"],
            "start": cue["start"],
            "end": cue["end"],
            "start_frame": cue["start_frame"],
            "end_frame": cue["end_frame"],
            "error_start_ms": cue["error_start_ms"],
            "error_end_ms": cue["error_end_ms"],
        }
        for cue in lyric_cues
    ]

    statuses = {line["status"] for line in lines}
    if "failed" in statuses:
        alignment_status = "failed"
    elif "estimated" in statuses:
        alignment_status = "estimated"
    else:
        alignment_status = str(alignment.get("alignment_status", alignment.get("status", "aligned")))

    timeline = {
        "schema_version": 3,
        "style_profile": style.get("reference_profile", {}).get("id", "pixel-narrative-mv"),
        "fps": fps,
        "duration": duration,
        "frame_count": frame_count,
        "width": int(render["width"]),
        "height": int(render["height"]),
        "low_width": int(render["low_width"]),
        "low_height": int(render["low_height"]),
        "scale": int(render["scale"]),
        "seed": top_seed,
        "art_version": 3,
        "story_theme": theme,
        "subtitle_band_height": int(style.get("subtitle", {}).get("band_height", 160)),
        "alignment_status": alignment_status,
        "sync": {
            "frame_clock": "shared_lyric_cue_v1",
            "interval": "left_closed_right_open",
            "quantization": "ceil_target_seconds_times_fps",
            "independent_shot_snap": False,
            "human_vocal_sync": "unverified",
        },
        "lyric_cues": lyric_cues,
        "shots": shots,
        "subtitles": subtitles,
    }
    _validate_timeline(timeline)
    return timeline


def _validate_timeline(timeline: dict) -> None:
    required = {
        "schema_version",
        "fps",
        "duration",
        "frame_count",
        "width",
        "height",
        "low_width",
        "low_height",
        "scale",
        "seed",
        "subtitle_band_height",
        "alignment_status",
        "sync",
        "lyric_cues",
        "shots",
        "subtitles",
    }
    missing = required.difference(timeline)
    if missing:
        raise ValueError(f"timeline 缺少字段：{', '.join(sorted(missing))}")
    fps = int(timeline["fps"])
    width = int(timeline["width"])
    height = int(timeline["height"])
    low_width = int(timeline["low_width"])
    low_height = int(timeline["low_height"])
    scale = int(timeline["scale"])
    frame_count = int(timeline["frame_count"])
    duration = _number(timeline["duration"], "timeline.duration")
    if (fps, width, height, low_width, low_height, scale) != (30, 1920, 1080, 480, 270, 4):
        raise ValueError("渲染规格必须为 30fps、480×270 低画布、4 倍放大和 1920×1080 输出")
    if frame_count != int(math.ceil(duration * fps)):
        raise ValueError("frame_count 必须等于 ceil(duration × fps)")
    shots = timeline["shots"]
    if not isinstance(shots, list) or not shots:
        raise ValueError("timeline.shots 必须是非空数组")
    cursor = 0
    for index, shot in enumerate(shots):
        start = int(shot["start_frame"])
        end = int(shot["end_frame"])
        if start != cursor or end <= start:
            raise ValueError(f"镜头 {index} 存在重叠、空洞或零长度")
        cursor = end
        if shot.get("scene") not in _SCENE_NAMES:
            shot["scene"] = "generic"
        if abs(float(shot["start"]) - start / fps) > 1e-6 or abs(float(shot["end"]) - end / fps) > 1e-6:
            raise ValueError(f"镜头 {shot.get('id', index)} 的秒时间必须与帧边界一致")
        if shot.get("weather") not in {"day", "cloudy", "rain", "snow", "night", "sunset"}:
            raise ValueError(f"镜头 {shot.get('id', index)} 使用未知天气")
        weather_frames = int(shot.get("weather_transition_frames", 0))
        if weather_frames < 0 or weather_frames > end - start:
            raise ValueError(f"镜头 {shot.get('id', index)} 的天气过渡帧数无效")
        if weather_frames and shot.get("weather_from") == shot.get("weather"):
            raise ValueError(f"镜头 {shot.get('id', index)} 的天气过渡起止状态不能相同")
    if cursor != frame_count:
        raise ValueError("镜头未连续覆盖全部视频帧")
    cues = timeline["lyric_cues"]
    subtitles = timeline["subtitles"]
    if not isinstance(cues, list) or not cues or len(cues) != len(subtitles):
        raise ValueError("lyric_cues 必须与字幕逐句一一对应")
    shot_at: list[int] = [-1] * frame_count
    for shot_index, shot in enumerate(shots):
        start_frame = int(shot["start_frame"])
        end_frame = int(shot["end_frame"])
        shot_at[start_frame:end_frame] = [shot_index] * (end_frame - start_frame)
    previous_start = -1
    for index, (cue, subtitle) in enumerate(zip(cues, subtitles)):
        start = int(cue["start_frame"])
        end = int(cue["end_frame"])
        if start < 0 or end > frame_count or end <= start or start <= previous_start:
            raise ValueError(f"歌词 cue {index} 的帧范围无效或不可区分")
        previous_start = start
        expected_start = min(frame_count, max(0, int(math.ceil(float(cue["target_start"]) * fps))))
        expected_end = min(frame_count, max(0, int(math.ceil(float(cue["target_end"]) * fps))))
        if (start, end) != (expected_start, expected_end):
            raise ValueError(f"歌词 cue {cue.get('id', index)} 必须统一使用 ceil(target × fps)")
        expected_ids = (f"lyric_{index:04d}", f"subtitle_{index:04d}")
        if (cue.get("id"), cue.get("subtitle_id")) != expected_ids:
            raise ValueError(f"歌词 cue {index} 的 ID 与字幕引用不一致")
        shared_fields = ("line_id", "text", "target_start", "target_end", "start", "end", "start_frame", "end_frame")
        if subtitle.get("id") != cue["subtitle_id"] or subtitle.get("cue_id") != cue["id"]:
            raise ValueError(f"字幕 {index} 未引用唯一歌词 cue")
        if any(subtitle.get(field) != cue.get(field) for field in shared_fields):
            raise ValueError(f"字幕 {subtitle.get('id', index)} 与歌词 cue 的时间或内容不一致")
        if not cue.get("shot_ids"):
            raise ValueError(f"歌词 cue {cue['id']} 没有对应叙事镜头")
        for frame in range(start, end):
            shot = shots[shot_at[frame]]
            if shot.get("line_id") != cue["line_id"] or shot.get("lyric_cue_id") != cue["id"]:
                raise ValueError(f"帧 {frame} 的歌词字幕与叙事镜头未绑定到同一句")
        first_shot = next(shot for shot in shots if shot["id"] == cue["shot_ids"][0])
        if int(first_shot["start_frame"]) != start:
            raise ValueError(f"歌词 cue {cue['id']} 的首镜头未从同一帧开始")


def _rgb(value: Any, palette: dict[str, str]) -> tuple[int, int, int]:
    if isinstance(value, str) and value in palette:
        value = palette[value]
    if isinstance(value, str):
        match = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
        if not match:
            raise ValueError(f"无法识别颜色：{value}")
        digits = match.group(1)
        return tuple(int(digits[offset : offset + 2], 16) for offset in (0, 2, 4))
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(max(0, min(255, int(channel))) for channel in value)
    raise ValueError(f"无法识别颜色：{value}")


def _palette_image(style: dict) -> Image.Image:
    colors = [_rgb(value, style.get("palette", {})) for value in style.get("palette", {}).values()]
    if not colors:
        colors = [(0, 0, 0), (255, 255, 255)]
    colors = list(dict.fromkeys(colors))[:256]
    flat = [channel for color in colors for channel in color]
    flat.extend([0] * (768 - len(flat)))
    image = Image.new("P", (1, 1))
    image.putpalette(flat)
    return image


def _quantize(image: Image.Image, palette_image: Image.Image, dither: float = 0.0) -> Image.Image:
    source = image.convert("RGB")
    if dither > 0:
        array = np.asarray(source, dtype=np.float32)
        height, width = array.shape[:2]
        matrix = np.tile((_BAYER_4 / 16.0 - 0.5) * (48.0 * dither), (math.ceil(height / 4), math.ceil(width / 4)))
        array = np.clip(array + matrix[:height, :width, None], 0, 255).astype(np.uint8)
        source = Image.fromarray(array)
    return source.quantize(palette=palette_image, dither=Image.Dither.NONE).convert("RGB")


def _apply_filter(image: Image.Image, name: str, style: dict) -> Image.Image:
    config = style.get("filters", {}).get(name, style.get("filters", {}).get("default", {}))
    palette = style.get("palette", {})
    result = image
    overlay = config.get("overlay")
    strength = float(config.get("strength", 0.0))
    if overlay and strength > 0:
        layer = Image.new("RGB", result.size, _rgb(overlay, palette))
        result = Image.blend(result, layer, min(1.0, strength))
    vignette = float(config.get("vignette", 0.0))
    if vignette > 0:
        array = np.asarray(result, dtype=np.float32)
        height, width = array.shape[:2]
        yy, xx = np.mgrid[0:height, 0:width]
        edge = np.maximum(np.abs(xx - width / 2) / (width / 2), np.abs(yy - height / 2) / (height / 2))
        alpha = np.clip((edge - 0.58) / 0.42, 0.0, 1.0)
        alpha = np.floor(alpha * 4.0) / 4.0 * min(1.0, vignette)
        ink = np.asarray(_rgb("ink", palette), dtype=np.float32)
        array = array * (1.0 - alpha[..., None]) + ink * alpha[..., None]
        result = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))
    return _quantize(result, palette_image=_palette_image(style), dither=float(config.get("dither", 0.0)))


def _rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: tuple[int, int, int]) -> None:
    draw.rectangle(tuple(int(value) for value in box), fill=fill)


def _halftone(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: tuple[int, int, int], step: int = 5) -> None:
    left, top, right, bottom = box
    for y in range(top, bottom, step):
        offset = (step // 2) if ((y - top) // step) % 2 else 0
        for x in range(left + offset, right, step):
            draw.point((x, y), fill=color)


def _cloud(draw: ImageDraw.ImageDraw, x: int, y: int, color: tuple[int, int, int]) -> None:
    _rect(draw, (x + 5, y, x + 18, y + 5), color)
    _rect(draw, (x, y + 5, x + 29, y + 11), color)
    _rect(draw, (x + 18, y + 3, x + 25, y + 8), color)


def _tree(draw: ImageDraw.ImageDraw, x: int, ground: int, colors: dict) -> None:
    _rect(draw, (x - 3, ground - 31, x + 3, ground), colors["wood"])
    for box in ((x - 18, ground - 54, x + 2, ground - 30), (x - 4, ground - 61, x + 18, ground - 34), (x - 13, ground - 67, x + 8, ground - 44)):
        _rect(draw, box, colors["leaf"])
    _rect(draw, (x - 10, ground - 58, x - 6, ground - 54), colors["mint"])


def _draw_scene_background(
    image: Image.Image,
    shot: dict,
    local_frame: int,
    style: dict,
    context: dict,
) -> None:
    draw = ImageDraw.Draw(image)
    colors = {name: _rgb(value, style.get("palette", {})) for name, value in style.get("palette", {}).items()}
    width, height = image.size
    scene = shot.get("scene", "generic")
    if scene not in _SCENE_NAMES:
        scene = "generic"
    hold = max(1, int(style.get("dynamics", {}).get("pose_hold_frames", 3)))
    parallax = int(style.get("dynamics", {}).get("parallax_pixels", 2))
    drift = ((local_frame // (hold * 4)) % 3 - 1) * parallax
    phase = (local_frame // hold) % 2
    cream, ink = colors["cream"], colors["ink"]

    if scene == "campus":
        _rect(draw, (0, 0, width, height), colors["sky"])
        _cloud(draw, 45 + drift, 28, cream)
        _cloud(draw, 350 - drift, 46, cream)
        _rect(draw, (0, 174, width, height), colors["leaf"])
        _rect(draw, (82, 70, 398, 180), cream)
        _rect(draw, (72, 65, 408, 78), colors["coral"])
        _rect(draw, (213, 45, 267, 70), cream)
        _rect(draw, (208, 40, 272, 48), colors["coral"])
        for y in (92, 126):
            for x in range(103, 373, 45):
                _rect(draw, (x, y, x + 25, y + 20), colors["indigo"])
                _rect(draw, (x + 3, y + 3, x + 22, y + 17), colors["sky"])
        _rect(draw, (222, 139, 258, 180), colors["wood"])
        _rect(draw, (0, 205, width, height), colors["coral"])
        for y in (218, 239, 260):
            draw.line((0, y, width, y), fill=cream, width=2)
        _tree(draw, 42 + drift, 194, colors)
        _halftone(draw, (0, 154, 80, 174), colors["green"], 4)

    elif scene == "bedroom":
        _rect(draw, (0, 0, width, height), cream)
        _rect(draw, (0, 190, width, height), colors["wood"])
        _rect(draw, (300, 35, 438, 137), colors["wood"])
        _rect(draw, (307, 42, 431, 130), colors["indigo"])
        _rect(draw, (366, 42, 372, 130), colors["wood"])
        _rect(draw, (307, 84, 431, 90), colors["wood"])
        _rect(draw, (390 + drift, 55, 407 + drift, 72), colors["amber"])
        _rect(draw, (35, 154, 225, 220), colors["wood"])
        _rect(draw, (43, 145, 217, 201), colors["mint"])
        _rect(draw, (47, 149, 104, 169), colors["warm_white"])
        _rect(draw, (43, 192, 217, 203), colors["green"])
        _rect(draw, (338, 158, 451, 174), colors["wood"])
        _rect(draw, (348, 174, 356, 224), colors["wood"])
        _rect(draw, (432, 174, 440, 224), colors["wood"])
        draw.polygon(((372, 157), (385, 128), (398, 157)), fill=colors["amber"])
        _rect(draw, (382, 156, 388, 169), colors["ink"])
        _halftone(draw, (15, 20, 270, 95), colors["pink"], 9)

    elif scene == "rain_street":
        _rect(draw, (0, 0, width, height), colors["indigo"])
        for index, (x, w, top) in enumerate(((0, 83, 55), (76, 102, 90), (172, 76, 40), (244, 120, 73), (358, 122, 30))):
            _rect(draw, (x, top, x + w, 190), colors["blue"] if index % 2 else colors["ink"])
            for wx in range(x + 12, x + w - 5, 24):
                for wy in range(top + 15, 174, 29):
                    if (wx + wy + index) % 3:
                        _rect(draw, (wx, wy, wx + 7, wy + 11), colors["amber"])
        _rect(draw, (0, 184, width, height), colors["gray"])
        _rect(draw, (0, 207, width, height), colors["indigo"])
        for x, color in ((54, colors["amber"]), (173, colors["red"]), (335, colors["sky"])):
            _rect(draw, (x, 211, x + 16, 217), color)
            _rect(draw, (x + 4, 222, x + 12, 247), color)
        _rect(draw, (63, 70, 69, 201), colors["ink"])
        _rect(draw, (49, 67, 82, 74), colors["ink"])
        _rect(draw, (56, 74, 75, 91), colors["amber"])
        draw.polygon(((45, 92), (86, 92), (105, 177), (28, 177)), fill=colors["blue"])

    elif scene == "seaside":
        _rect(draw, (0, 0, width, height), colors["sky"])
        _rect(draw, (0, 82, width, 151), colors["coral"])
        _rect(draw, (0, 128, width, 158), colors["amber"])
        _rect(draw, (347 + drift, 71, 383 + drift, 107), colors["warm_white"])
        _rect(draw, (0, 150, width, 222), colors["sea"])
        for y in (162, 181, 204):
            for x in range(-20, width, 65):
                offset = drift + (phase * 5 if y == 181 else 0)
                _rect(draw, (x + offset, y, x + 35 + offset, y + 3), cream)
        _rect(draw, (0, 220, width, height), cream)
        draw.polygon(((0, 151), (46, 116), (96, 151)), fill=colors["indigo"])
        _halftone(draw, (0, 235, width, height), colors["amber"], 7)
        _cloud(draw, 80 + drift, 38, cream)

    elif scene == "station":
        _rect(draw, (0, 0, width, height), cream)
        _rect(draw, (0, 26, width, 57), colors["ink"])
        _rect(draw, (0, 57, width, 64), colors["coral"])
        for x in (32, 211, 422):
            _rect(draw, (x, 54, x + 10, 229), colors["gray"])
        _rect(draw, (54, 94, 434, 181), colors["sea"])
        _rect(draw, (54, 142, 434, 153), colors["coral"])
        for x in range(75, 385, 63):
            _rect(draw, (x, 107, x + 43, 134), colors["indigo"])
            _rect(draw, (x + 4, 111, x + 39, 130), colors["sky"])
        for x in (97, 370):
            _rect(draw, (x, 177, x + 27, 187), colors["ink"])
        _rect(draw, (0, 188, width, 232), colors["wood"])
        _rect(draw, (0, 232, width, height), colors["gray"])
        for y in (240, 258):
            draw.line((0, y, width, y), fill=ink, width=3)
        for x in range(-10, width, 36):
            draw.line((x, 231, x + 17, 270), fill=colors["wood"], width=3)
        _rect(draw, (224 + drift, 65, 258 + drift, 91), colors["warm_white"])
        draw.line((241 + drift, 69, 241 + drift, 80), fill=ink, width=2)
        draw.line((241 + drift, 80, 249 + drift, 84), fill=ink, width=2)

    elif scene == "stage":
        _rect(draw, (0, 0, width, height), colors["indigo"])
        draw.polygon(((74 + drift, 30), (104 + drift, 30), (196, 211), (20, 211)), fill=colors["violet"])
        draw.polygon(((370 - drift, 30), (400 - drift, 30), (461, 211), (284, 211)), fill=colors["blue"])
        _rect(draw, (0, 0, 42, 199), colors["violet"])
        _rect(draw, (438, 0, width, 199), colors["violet"])
        for x in range(0, width, 45):
            draw.polygon(((x, 0), (x + 43, 0), (x + 34, 29), (x + 8, 29)), fill=colors["violet"])
        _rect(draw, (0, 195, width, height), colors["wood"])
        for y in range(205, height, 16):
            draw.line((0, y, width, y), fill=colors["ink"], width=2)
        lamp_colors = (colors["pink"], colors["amber"], colors["sky"], colors["coral"])
        for index, x in enumerate(range(72, 433, 45)):
            color = lamp_colors[(index + phase) % len(lamp_colors)]
            _rect(draw, (x, 25, x + 14, 35), color)
            if (index + phase) % 2 == 0:
                _rect(draw, (x + 4, 38, x + 10, 42), color)
        for x in range(15, width, 31):
            _rect(draw, (x, 244 - (x % 9), x + 14, height), colors["ink"])

    elif scene == "highway":
        _rect(draw, (0, 0, width, height), colors["coral"])
        _rect(draw, (0, 0, width, 68), colors["sky"])
        _rect(draw, (350 + drift, 66, 392 + drift, 108), colors["amber"])
        draw.polygon(((0, 149), (52, 100), (99, 137), (145, 85), (206, 150)), fill=colors["violet"])
        draw.polygon(((303, 150), (348, 114), (385, 135), (431, 93), (480, 141), (480, 170)), fill=colors["violet"])
        _rect(draw, (0, 150, width, height), colors["amber"])
        draw.polygon(((228, 150), (255, 150), (340, height), (128, height)), fill=colors["ink"])
        draw.line((228, 150, 128, height), fill=cream, width=3)
        draw.line((255, 150, 340, height), fill=cream, width=3)
        for index in range(6):
            y = 158 + ((index * 24 + (local_frame // hold) * 3) % 130)
            if y >= height:
                continue
            perspective = max(1, (y - 145) // 35)
            x = 241 + (y - 150) // 12
            _rect(draw, (x, y, x + perspective, min(height, y + 6 + perspective * 2)), cream)
        _rect(draw, (48 - drift, 168, 80 - drift, 189), colors["wood"])
        _rect(draw, (58 - drift, 150, 70 - drift, 174), colors["green"])

    elif scene == "ancient":
        _rect(draw, (0, 0, width, height), colors["indigo"])
        _rect(draw, (354 + drift, 35, 391 + drift, 72), cream)
        _rect(draw, (361 + drift, 31, 398 + drift, 68), colors["indigo"])
        draw.polygon(((0, 161), (78, 72), (145, 158), (223, 59), (322, 162), (403, 86), (480, 157), (480, 197), (0, 197)), fill=colors["gray"])
        draw.polygon(((0, 180), (99, 119), (187, 183), (283, 116), (394, 184), (480, 137), (480, 207), (0, 207)), fill=colors["ink"])
        _rect(draw, (0, 193, width, height), cream)
        _rect(draw, (88, 127, 393, 201), colors["wood"])
        _rect(draw, (102, 139, 379, 201), cream)
        draw.polygon(((64, 132), (121, 102), (364, 102), (418, 132)), fill=colors["ink"])
        _rect(draw, (76, 132, 406, 140), colors["gray"])
        for x in (121, 208, 294, 366):
            _rect(draw, (x, 140, x + 9, 204), colors["red"])
        for x in (153, 327):
            _rect(draw, (x, 151, x + 16, 172), colors["red"])
            _rect(draw, (x + 5, 172, x + 11, 181), colors["amber"])
        _halftone(draw, (0, 218, width, height), colors["gray"], 8)

    else:
        _rect(draw, (0, 0, width, height), cream)
        _rect(draw, (0, 0, width, 129), colors["sky"])
        _cloud(draw, 70 + drift, 34, cream)
        _cloud(draw, 341 - drift, 55, cream)
        _rect(draw, (355 + drift, 70, 387 + drift, 102), colors["coral"])
        draw.polygon(((0, 157), (86, 96), (171, 157), (278, 86), (402, 160), (480, 119), (480, 209), (0, 209)), fill=colors["mint"])
        draw.polygon(((0, 185), (116, 137), (234, 189), (360, 128), (480, 184), (480, 224), (0, 224)), fill=colors["leaf"])
        _rect(draw, (0, 211, width, height), colors["wood"])
        _rect(draw, (17, 18, 25, 189), cream)
        _rect(draw, (455, 18, 463, 189), cream)
        _halftone(draw, (29, 140, 108, 182), colors["green"], 5)

    if shot.get("filter") == "stage" and scene != "stage":
        particle_colors = (colors["amber"], colors["pink"], colors["warm_white"])
        for index, (x, y) in enumerate(context["particles"]):
            if (index + local_frame // hold) % 3:
                color = particle_colors[index % len(particle_colors)]
                _rect(draw, (int(x), int(y), int(x) + 2, int(y) + 2), color)


def _draw_cat(draw: ImageDraw.ImageDraw, x: int, ground: int, colors: dict, pose: int = 0, scale: int = 1) -> None:
    s = max(1, int(scale))
    body = colors["amber"]
    accent = colors["cream"]
    ink = colors["ink"]
    _rect(draw, (x - 8 * s, ground - 9 * s, x + 6 * s, ground - 2 * s), body)
    _rect(draw, (x - 6 * s, ground - 16 * s, x + 5 * s, ground - 7 * s), body)
    draw.polygon(((x - 6 * s, ground - 15 * s), (x - 4 * s, ground - 21 * s), (x - 1 * s, ground - 15 * s)), fill=body)
    draw.polygon(((x + 1 * s, ground - 15 * s), (x + 4 * s, ground - 21 * s), (x + 5 * s, ground - 14 * s)), fill=body)
    _rect(draw, (x - 3 * s, ground - 8 * s, x + 4 * s, ground - 3 * s), accent)
    _rect(draw, (x - 3 * s, ground - 13 * s, x - 2 * s, ground - 12 * s), ink)
    _rect(draw, (x + 2 * s, ground - 13 * s, x + 3 * s, ground - 12 * s), ink)
    tail_y = ground - (12 + pose * 3) * s
    _rect(draw, (x + 6 * s, tail_y, x + 9 * s, ground - 4 * s), body)
    _rect(draw, (x + 9 * s, tail_y, x + 12 * s, tail_y + 3 * s), body)
    _rect(draw, (x - 7 * s, ground - 2 * s, x - 3 * s, ground), ink)
    _rect(draw, (x + 2 * s, ground - 2 * s, x + 6 * s, ground), ink)


def _draw_character(
    draw: ImageDraw.ImageDraw,
    name: str,
    x: int,
    ground: int,
    style: dict,
    pose: int,
    blink: bool,
) -> None:
    config = style.get("characters", {}).get(name, {})
    palette = style.get("palette", {})
    height = int(config.get("height_pixels", 48 if name == "protagonist" else 46))
    hair = _rgb(config.get("hair", "wood"), palette)
    coat = _rgb(config.get("coat", "coral"), palette)
    trousers = _rgb(config.get("trousers", "blue"), palette)
    accent = _rgb(config.get("accent", "cream"), palette)
    skin = _rgb("pink", palette)
    ink = _rgb("ink", palette)
    head_top = ground - height
    head_size = 19 if name == "protagonist" else 18
    _rect(draw, (x - head_size // 2, head_top + 4, x + head_size // 2, head_top + 4 + head_size), skin)
    _rect(draw, (x - head_size // 2 - 1, head_top, x + head_size // 2 + 1, head_top + 8), hair)
    _rect(draw, (x - head_size // 2 - 2, head_top + 5, x - head_size // 2 + 1, head_top + 15), hair)
    if name == "companion":
        _rect(draw, (x + head_size // 2 - 1, head_top + 5, x + head_size // 2 + 3, head_top + 20), hair)
    eye_y = head_top + 12
    eye_height = 1 if blink else 3
    _rect(draw, (x - 5, eye_y, x - 3, eye_y + eye_height), ink)
    _rect(draw, (x + 3, eye_y, x + 5, eye_y + eye_height), ink)
    _rect(draw, (x - 2, eye_y + 7, x + 2, eye_y + 8), accent)
    body_top = head_top + head_size + 5
    body_bottom = ground - 12
    _rect(draw, (x - 9, body_top, x + 9, body_bottom), coat)
    _rect(draw, (x - 2, body_top, x + 2, body_bottom), accent)
    if pose:
        _rect(draw, (x - 14, body_top - 5, x - 9, body_top + 5), coat)
        _rect(draw, (x - 15, body_top - 9, x - 11, body_top - 4), skin)
        _rect(draw, (x + 9, body_top + 2, x + 13, body_bottom - 2), coat)
    else:
        _rect(draw, (x - 13, body_top + 2, x - 9, body_bottom - 2), coat)
        _rect(draw, (x + 9, body_top + 2, x + 13, body_bottom - 5), coat)
    leg_offset = 2 if pose else 0
    _rect(draw, (x - 7 - leg_offset, body_bottom, x - 1 - leg_offset, ground - 3), trousers)
    _rect(draw, (x + 1 + leg_offset, body_bottom, x + 7 + leg_offset, ground - 3), trousers)
    _rect(draw, (x - 9 - leg_offset, ground - 3, x - 1 - leg_offset, ground), ink)
    _rect(draw, (x + 1 + leg_offset, ground - 3, x + 9 + leg_offset, ground), ink)


def _draw_prop(
    draw: ImageDraw.ImageDraw,
    name: str,
    x: int,
    ground: int,
    style: dict,
    pose: int = 0,
    scale: int = 1,
) -> None:
    colors = {key: _rgb(value, style.get("palette", {})) for key, value in style.get("palette", {}).items()}
    s = max(1, int(scale))
    ink, cream = colors["ink"], colors["cream"]
    if name == "cat":
        _draw_cat(draw, x, ground, colors, pose, s)
        return
    if name == "letter":
        _rect(draw, (x - 12 * s, ground - 15 * s, x + 12 * s, ground), cream)
        draw.rectangle((x - 12 * s, ground - 15 * s, x + 12 * s, ground), outline=colors["wood"], width=s)
        draw.line((x - 12 * s, ground - 15 * s, x, ground - 6 * s, x + 12 * s, ground - 15 * s), fill=colors["wood"], width=s)
        _rect(draw, (x + 6 * s, ground - 5 * s, x + 9 * s, ground - 2 * s), colors["red"])
    elif name == "photo":
        _rect(draw, (x - 12 * s, ground - 22 * s, x + 12 * s, ground), colors["wood"])
        _rect(draw, (x - 9 * s, ground - 19 * s, x + 9 * s, ground - 3 * s), colors["sky"])
        draw.polygon(((x - 8 * s, ground - 4 * s), (x - 1 * s, ground - 13 * s), (x + 7 * s, ground - 4 * s)), fill=colors["mint"])
        _rect(draw, (x + 3 * s, ground - 16 * s, x + 6 * s, ground - 13 * s), colors["amber"])
    elif name == "guitar":
        _rect(draw, (x - 6 * s, ground - 17 * s, x + 5 * s, ground - 3 * s), colors["wood"])
        _rect(draw, (x - 9 * s, ground - 12 * s, x + 8 * s, ground - 2 * s), colors["amber"])
        _rect(draw, (x - 2 * s, ground - 31 * s, x + 2 * s, ground - 15 * s), colors["wood"])
        _rect(draw, (x - 4 * s, ground - 34 * s, x + 4 * s, ground - 29 * s), ink)
        _rect(draw, (x - 2 * s, ground - 11 * s, x + 2 * s, ground - 7 * s), ink)
        draw.line((x, ground - 30 * s, x, ground - 4 * s), fill=cream, width=max(1, s // 2))
    elif name == "phone":
        _rect(draw, (x - 7 * s, ground - 25 * s, x + 7 * s, ground), ink)
        _rect(draw, (x - 5 * s, ground - 21 * s, x + 5 * s, ground - 5 * s), colors["sky"])
        _rect(draw, (x - 2 * s, ground - 3 * s, x + 2 * s, ground - 2 * s), cream)
        _rect(draw, (x - 3 * s, ground - 17 * s, x + 3 * s, ground - 15 * s), cream)
    elif name == "ticket":
        _rect(draw, (x - 15 * s, ground - 13 * s, x + 15 * s, ground), cream)
        draw.rectangle((x - 15 * s, ground - 13 * s, x + 15 * s, ground), outline=colors["wood"], width=s)
        _rect(draw, (x - 15 * s, ground - 13 * s, x - 10 * s, ground), colors["coral"])
        for dx in range(-6, 13, 4):
            _rect(draw, (x + dx * s, ground - 8 * s, x + (dx + 2) * s, ground - 7 * s), ink)
        draw.line((x + 7 * s, ground - 12 * s, x + 7 * s, ground - s), fill=colors["wood"], width=s)
    elif name == "suitcase":
        _rect(draw, (x - 12 * s, ground - 23 * s, x + 12 * s, ground - 3 * s), colors["coral"])
        _rect(draw, (x - 5 * s, ground - 29 * s, x + 5 * s, ground - 25 * s), ink)
        _rect(draw, (x - 3 * s, ground - 26 * s, x + 3 * s, ground - 23 * s), cream)
        _rect(draw, (x - 7 * s, ground - 20 * s, x - 4 * s, ground - 6 * s), colors["amber"])
        _rect(draw, (x + 4 * s, ground - 20 * s, x + 7 * s, ground - 6 * s), colors["amber"])
        _rect(draw, (x - 9 * s, ground - 2 * s, x - 5 * s, ground), ink)
        _rect(draw, (x + 5 * s, ground - 2 * s, x + 9 * s, ground), ink)
    elif name == "player":
        _rect(draw, (x - 16 * s, ground - 20 * s, x + 16 * s, ground), colors["gray"])
        _rect(draw, (x - 12 * s, ground - 16 * s, x + 12 * s, ground - 5 * s), ink)
        for dx in (-6, 6):
            _rect(draw, (x + (dx - 3) * s, ground - 13 * s, x + (dx + 3) * s, ground - 7 * s), cream)
            _rect(draw, (x + (dx - 1) * s, ground - 11 * s, x + (dx + 1) * s, ground - 9 * s), colors["coral"])
        _rect(draw, (x - 11 * s, ground - 3 * s, x - 5 * s, ground - s), colors["red"])
    elif name == "scroll":
        _rect(draw, (x - 13 * s, ground - 22 * s, x + 13 * s, ground - 2 * s), cream)
        draw.rectangle((x - 13 * s, ground - 22 * s, x + 13 * s, ground - 2 * s), outline=colors["wood"], width=s)
        _rect(draw, (x - 16 * s, ground - 24 * s, x - 12 * s, ground), colors["wood"])
        _rect(draw, (x + 12 * s, ground - 24 * s, x + 16 * s, ground), colors["wood"])
        for dy in (-16, -11, -6):
            _rect(draw, (x - 7 * s, ground + dy * s, x + (5 + dy % 3) * s, ground + (dy + 1) * s), ink)
    elif name == "bicycle":
        for cx in (x - 13 * s, x + 13 * s):
            draw.rectangle((cx - 8 * s, ground - 16 * s, cx + 8 * s, ground), outline=ink, width=2 * s)
        draw.line((x - 13 * s, ground - 8 * s, x - 2 * s, ground - 20 * s, x + 4 * s, ground - 8 * s, x - 13 * s, ground - 8 * s, x + 13 * s, ground - 8 * s, x + 7 * s, ground - 22 * s), fill=colors["coral"], width=s)
        draw.line((x - 5 * s, ground - 21 * s, x + 1 * s, ground - 21 * s), fill=ink, width=2 * s)
        draw.line((x + 4 * s, ground - 24 * s, x + 11 * s, ground - 24 * s), fill=ink, width=s)
    elif name == "microphone":
        _rect(draw, (x - 4 * s, ground - 34 * s, x + 4 * s, ground - 23 * s), colors["gray"])
        _rect(draw, (x - 2 * s, ground - 24 * s, x + 2 * s, ground - 4 * s), ink)
        _rect(draw, (x - 10 * s, ground - 3 * s, x + 10 * s, ground), ink)
        for dy in (-31, -27):
            _rect(draw, (x - 3 * s, ground + dy * s, x + 3 * s, ground + (dy + 1) * s), cream)
    elif name == "red_umbrella":
        draw.polygon(((x - 19 * s, ground - 20 * s), (x - 13 * s, ground - 29 * s), (x, ground - 34 * s), (x + 13 * s, ground - 29 * s), (x + 19 * s, ground - 20 * s)), fill=colors["red"])
        _rect(draw, (x - 19 * s, ground - 21 * s, x + 19 * s, ground - 18 * s), colors["coral"])
        _rect(draw, (x - s, ground - 20 * s, x + s, ground - 2 * s), ink)
        _rect(draw, (x, ground - 3 * s, x + 6 * s, ground - s), ink)
        _rect(draw, (x + 4 * s, ground - 5 * s, x + 7 * s, ground - s), ink)
    else:
        _rect(draw, (x - 8 * s, ground - 12 * s, x + 8 * s, ground), colors["coral"])
        _rect(draw, (x - 5 * s, ground - 9 * s, x + 5 * s, ground - 3 * s), cream)


def _shot_context(shot: dict, style: dict) -> dict:
    rng = np.random.default_rng(int(shot.get("seed", 0)))
    dynamics = style.get("dynamics", {})
    rain_count = max(0, int(dynamics.get("rain_drops", 36)))
    particle_count = max(0, int(dynamics.get("particle_count", 24)))
    return {
        "rain": np.column_stack((rng.integers(0, 480, rain_count), rng.integers(0, 225, rain_count), rng.integers(3, 7, rain_count))),
        "particles": np.column_stack((rng.integers(18, 462, particle_count), rng.integers(30, 195, particle_count))),
    }


def _apply_camera(image: Image.Image, shot: dict, local_frame: int) -> Image.Image:
    camera = shot.get("camera", {})
    kind = str(camera.get("kind", "static"))
    if kind == "static" or image.size != (480, 270):
        return image
    total = max(1, int(shot.get("end_frame", 1)) - int(shot.get("start_frame", 0)) - 1)
    hold = max(1, int(camera.get("hold_frames", 2)))
    progress = min(1.0, (local_frame // hold) / max(1, total // hold))
    maximum = max(0, int(camera.get("max_crop_pixels", 12)))
    if kind == "push_in":
        margin = int(maximum * progress)
    elif kind == "pull_out":
        margin = int(maximum * (1.0 - progress))
    else:
        margin = maximum
    if margin <= 0:
        return image
    vertical = int(round(margin * 9 / 16))
    crop_width = 480 - margin * 2
    crop_height = 270 - vertical * 2
    x0 = margin
    if kind in ("pan_left", "pan_right"):
        pan = min(margin, max(0, int(camera.get("pan_pixels", 8))))
        direction = -1 if kind == "pan_left" else 1
        x0 = margin + int(round((progress * 2 - 1) * pan * direction))
    x0 = max(0, min(480 - crop_width, x0))
    return image.crop((x0, vertical, x0 + crop_width, vertical + crop_height)).resize(
        (480, 270), Image.Resampling.NEAREST
    )


def _render_low_frame(shot: dict, local_frame: int, style: dict, context: dict) -> Image.Image:
    if int(shot.get("art_version", 1)) >= 2:
        image = art.render_art_frame(shot, local_frame, style, context)
        image = _apply_camera(image, shot, local_frame)
        return _apply_filter(image, str(shot.get("filter", "default")), style)
    image = Image.new("RGB", (480, 270), _rgb("cream", style.get("palette", {})))
    _draw_scene_background(image, shot, local_frame, style, context)
    draw = ImageDraw.Draw(image)
    colors = {name: _rgb(value, style.get("palette", {})) for name, value in style.get("palette", {}).items()}
    hold = max(1, int(style.get("dynamics", {}).get("pose_hold_frames", 3)))
    pose = (local_frame // hold) % 2
    blink_frames = max(0, int(style.get("dynamics", {}).get("blink_frames", 2)))
    blink = blink_frames > 0 and local_frame % (hold * 16) < blink_frames
    characters = list(shot.get("characters", ["protagonist"]))
    props = list(shot.get("props", []))
    if not characters and props:
        spacing = 480 // (len(props) + 1)
        for index, name in enumerate(props):
            _draw_prop(draw, name, spacing * (index + 1), 211, style, pose, 2)
    else:
        positions = ((65, 215), (407, 215), (137, 226))
        for index, name in enumerate(props[:3]):
            _draw_prop(draw, name, positions[index][0], positions[index][1], style, pose, 1)
    if "protagonist" in characters:
        _draw_character(draw, "protagonist", 237, 226, style, pose, blink)
    if "companion" in characters:
        _draw_character(draw, "companion", 290, 226, style, 1 - pose, blink)
    if "cat" in characters and "cat" not in props:
        _draw_cat(draw, 337, 226, colors, pose)

    if shot.get("scene") == "rain_street":
        rain_color = colors["sky"]
        for index, (base_x, base_y, speed) in enumerate(context["rain"]):
            x = int((base_x + local_frame + index) % 480)
            y = int((base_y + local_frame * int(speed)) % 225)
            draw.line((x, y, x - 2, y + 7), fill=rain_color, width=1)
    return _apply_filter(image, str(shot.get("filter", "default")), style)


def _transition(
    current: Image.Image,
    previous: Image.Image,
    name: str,
    progress: float,
    palette_image: Image.Image,
) -> Image.Image:
    progress = max(0.0, min(1.0, progress))
    if name == "white_flash":
        white = Image.new("RGB", current.size, (255, 249, 240))
        return _quantize(Image.blend(current, white, 1.0 - progress), palette_image)
    if name == "iris_out":
        mask = Image.new("L", current.size, 0)
        draw = ImageDraw.Draw(mask)
        radius = math.hypot(current.width, current.height) / 2 * (1.0 - progress)
        cx, cy = current.width // 2, current.height // 2
        draw.ellipse((int(cx - radius), int(cy - radius), int(cx + radius), int(cy + radius)), fill=255)
        return Image.composite(current, previous, mask)
    if name == "scroll_wipe":
        cut = int(current.width * progress)
        old = np.asarray(previous)
        new = np.asarray(current)
        output = old.copy()
        if cut:
            output[:, current.width - cut :] = new[:, :cut]
        return Image.fromarray(output)
    if name == "memory_dissolve":
        bayer = np.array(((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5)), dtype=np.float32)
        threshold = np.tile(bayer, (math.ceil(current.height / 4), math.ceil(current.width / 4)))[: current.height, : current.width]
        mask = Image.fromarray(np.where(threshold < progress * 16.0, 255, 0).astype(np.uint8), mode="L")
        return Image.composite(current, previous, mask)
    if name == "film_wipe":
        cut = int(current.width * progress)
        output = previous.copy()
        if cut:
            output.paste(current.crop((0, 0, cut, current.height)), (0, 0))
        draw = ImageDraw.Draw(output)
        edge = max(0, min(current.width - 1, cut))
        draw.rectangle((max(0, edge - 5), 0, min(current.width - 1, edge + 5), current.height), fill=(23, 21, 28))
        for y in range(5, current.height, 18):
            draw.rectangle((max(0, edge - 3), y, min(current.width - 1, edge + 3), y + 7), fill=(244, 235, 221))
        return output
    if name == "photo_reveal":
        if progress >= 1.0:
            return current
        output = previous.copy()
        width = max(12, int(current.width * progress))
        height = max(7, int(current.height * progress))
        reveal = current.resize((width, height), Image.Resampling.NEAREST)
        x = (current.width - width) // 2
        y = (current.height - height) // 2
        draw = ImageDraw.Draw(output)
        border = max(2, int(8 * progress))
        draw.rectangle((x - border, y - border, x + width + border, y + height + border), fill=(244, 235, 221))
        output.paste(reveal, (x, y))
        return output
    if name == "match_cut":
        if progress > 0.5:
            return current
        margin = int(10 * progress * 2)
        if margin <= 0:
            return previous
        crop = previous.crop((margin, int(margin * 9 / 16), previous.width - margin, previous.height - int(margin * 9 / 16)))
        return crop.resize(current.size, Image.Resampling.NEAREST)
    return current


def _luminance(color: tuple[int, int, int]) -> float:
    channels = []
    for value in color:
        srgb = value / 255.0
        channels.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    a, b = _luminance(first), _luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_chars: int,
) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    tokens = re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\s+|.", text, flags=re.DOTALL)
    lines: list[str] = []
    current = ""

    def fits(value: str) -> bool:
        if len(value) > max_chars:
            return False
        box = draw.textbbox((0, 0), value, font=font)
        return box[2] - box[0] <= max_width

    for token in tokens:
        if token.isspace() and not current:
            continue
        candidate = current + token
        if fits(candidate):
            current = candidate
            continue
        if current.strip():
            lines.append(current.strip())
            current = ""
        token = token.strip()
        if not token:
            continue
        if fits(token):
            current = token
            continue
        for char in token:
            candidate = current + char
            if current and not fits(candidate):
                lines.append(current)
                current = char
            else:
                current = candidate
    if current.strip():
        lines.append(current.strip())
    return lines or [""]


def _prepare_subtitle(
    subtitle: dict,
    style: dict,
    font_path: str,
    width: int,
    height: int,
) -> dict:
    config = style.get("subtitle", {})
    palette = style.get("palette", {})
    background = _rgb(config.get("background", "cream"), palette)
    text_color = _rgb(config.get("text_color", "ink"), palette)
    accent = _rgb(config.get("accent_color", "red"), palette)
    border = _rgb(config.get("border_color", "coral"), palette)
    contrast = _contrast(background, text_color)
    if contrast < float(config.get("min_contrast", 4.5)):
        raise ValueError(f"字幕对比度 {contrast:.2f} 低于 min_contrast")
    safe = max(0, int(config.get("safe_margin", 48)))
    padding_x = max(8, int(config.get("box_padding_x", 24)))
    padding_y = max(6, int(config.get("box_padding_y", 14)))
    note_gap = max(4, int(config.get("note_gap", 18)))
    max_box_width = min(int(config.get("max_box_width", 1240)), width - 2 * safe)
    min_box_width = min(max_box_width, max(0, int(config.get("min_box_width", 280))))
    if max_box_width <= 2 * padding_x + 80:
        raise ValueError("字幕安全区尺寸无效")
    max_lines = max(1, int(config.get("max_lines", 2)))
    configured_chars = max(1, int(config.get("max_chars_per_line", 24)))
    spacing = max(0, int(config.get("line_spacing", 12)))
    initial_size = max(8, int(config.get("font_size", 42)))
    minimum_size = max(8, min(initial_size, int(config.get("min_font_size", 16))))
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    text = re.sub(r"\s+", " ", str(subtitle.get("text", ""))).strip()
    selected = None
    for relaxed in (False, True):
        char_limit = configured_chars if not relaxed else max(configured_chars, int(math.ceil(len(text) / max_lines)))
        for size in range(initial_size, minimum_size - 1, -2):
            font = ImageFont.truetype(font_path, size)
            note_height = max(24, size)
            note_width = max(18, int(note_height * 0.72))
            max_text_width = max_box_width - 2 * padding_x - note_width - note_gap
            lines = _wrap_text(probe, text, font, max_text_width, char_limit)
            if len(lines) > max_lines:
                continue
            boxes = [probe.textbbox((0, 0), line, font=font) for line in lines]
            heights = [box[3] - box[1] for box in boxes]
            widths = [box[2] - box[0] for box in boxes]
            block_height = sum(heights) + spacing * (len(lines) - 1)
            selected = (font, lines, boxes, heights, widths, block_height, note_width, note_height)
            break
        if selected:
            break
    if selected is None:
        raise ValueError(f"字幕“{text[:24]}”过长，无法在 max_lines 和安全区内完整显示")
    font, lines, boxes, heights, widths, block_height, note_width, note_height = selected
    box_width = min(
        max_box_width,
        max(min_box_width, max(widths, default=0) + note_width + note_gap + 2 * padding_x),
    )
    box_height = block_height + 2 * padding_y
    bottom_margin = max(safe, int(config.get("bottom_margin", 36)))
    x0 = (width - box_width) // 2
    y0 = height - bottom_margin - box_height
    x1, y1 = x0 + box_width, y0 + box_height
    if x0 < safe or x1 > width - safe or y0 < safe or y1 > height - safe:
        raise ValueError(f"字幕“{text[:24]}”超出安全区")
    note_x = x0 + padding_x
    note_y = y0 + (box_height - note_height) // 2
    note_bounds = (note_x, note_y, note_x + note_width, note_y + note_height)
    text_left = note_x + note_width + note_gap
    text_right = x1 - padding_x
    y = y0 + padding_y
    line_items = []
    for line, box, line_height, line_width in zip(lines, boxes, heights, widths):
        x = text_left + (text_right - text_left - line_width) // 2
        origin = (int(x - box[0]), int(y - box[1]))
        bounds = (int(x), int(y), int(x + line_width), int(y + line_height))
        if bounds[0] < text_left or bounds[2] > text_right or bounds[1] < y0 or bounds[3] > y1:
            raise ValueError(f"字幕“{text[:24]}”超出字幕框")
        line_items.append((line, origin, bounds))
        y += line_height + spacing
    return {
        "background": background,
        "text_color": text_color,
        "accent": accent,
        "border": border,
        "border_width": max(1, int(config.get("border_width", 4))),
        "corner_radius": max(0, int(config.get("corner_radius", 8))),
        "contrast": contrast,
        "band_top": height - int(config.get("band_height", 160)),
        "box_bounds": (x0, y0, x1, y1),
        "font": font,
        "lines": line_items,
        "note_bounds": note_bounds,
        "overflow": False,
    }


def _draw_subtitle(image: Image.Image, prepared: dict) -> None:
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        prepared["box_bounds"],
        fill=prepared["background"],
        outline=prepared["border"],
        width=prepared["border_width"],
    )
    for text, origin, _ in prepared["lines"]:
        draw.text(origin, text, font=prepared["font"], fill=prepared["text_color"])
    x0, y0, x1, y1 = prepared["note_bounds"]
    width = x1 - x0
    height = y1 - y0
    stem_x = x0 + int(width * 0.58)
    stem_width = max(3, height // 14)
    draw.ellipse(
        (x0, y0 + int(height * 0.62), x0 + int(width * 0.68), y1 - int(height * 0.06)),
        fill=prepared["accent"],
    )
    draw.rectangle(
        (stem_x, y0 + int(height * 0.08), stem_x + stem_width, y0 + int(height * 0.76)),
        fill=prepared["accent"],
    )
    draw.polygon(
        (
            (stem_x, y0),
            (x1, y0 + int(height * 0.16)),
            (x1, y0 + int(height * 0.38)),
            (stem_x + stem_width, y0 + int(height * 0.25)),
        ),
        fill=prepared["accent"],
    )


def _keyframe_requests(shots: list[dict], directory: Path | None) -> dict[int, list[Path]]:
    if directory is None:
        return {}
    requests: dict[int, list[Path]] = {}
    for index, shot in enumerate(shots):
        start = int(shot["start_frame"])
        end = int(shot["end_frame"]) - 1
        middle = (start + end) // 2
        for label, frame in (("start", start), ("mid", middle), ("end", end)):
            path = directory / f"shot_{index:04d}_{label}.png"
            requests.setdefault(frame, []).append(path)
    return requests


def build_frame_bindings(timeline: dict) -> list[dict]:
    """从唯一 cue/镜头时间轴派生逐帧绑定，渲染与测试共用此映射。"""
    frame_count = int(timeline["frame_count"])
    shot_at: list[int] = [-1] * frame_count
    for index, shot in enumerate(timeline["shots"]):
        start_frame = int(shot["start_frame"])
        end_frame = int(shot["end_frame"])
        shot_at[start_frame:end_frame] = [index] * (end_frame - start_frame)
    cue_at: list[dict | None] = [None] * frame_count
    for cue in timeline["lyric_cues"]:
        for frame in range(int(cue["start_frame"]), int(cue["end_frame"])):
            cue_at[frame] = cue
    bindings = []
    for frame in range(frame_count):
        shot_index = shot_at[frame]
        shot = timeline["shots"][shot_index]
        cue = cue_at[frame]
        local_frame = frame - int(shot["start_frame"])
        weather = art.weather_state(shot, local_frame)
        bindings.append(
            {
                "frame": frame,
                "pts": frame / int(timeline["fps"]),
                "shot_index": shot_index,
                "shot_id": shot["id"],
                "local_frame": local_frame,
                "visual_line_id": cue["line_id"] if cue else None,
                "lyric_cue_id": cue["id"] if cue else None,
                "lyric_line_id": cue["line_id"] if cue else None,
                "subtitle_id": cue["subtitle_id"] if cue else None,
                "weather": shot["weather"],
                "weather_from": shot.get("weather_from"),
                "weather_transition": weather["active"],
                "weather_transition_stage": weather["stage"],
                "weather_transition_steps": weather["steps"],
                "weather_mix": weather["amount"],
            }
        )
    return bindings


def render_video(
    timeline: dict,
    style: dict,
    output_path: str | Path,
    frames_log_path: str | Path,
    keyframe_dir: str | Path | None = None,
) -> dict:
    _validate_timeline(timeline)
    output = Path(output_path)
    log_path = Path(frames_log_path)
    keyframes = Path(keyframe_dir) if keyframe_dir is not None else None
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if keyframes is not None:
        keyframes.mkdir(parents=True, exist_ok=True)

    fps = int(timeline["fps"])
    width = int(timeline["width"])
    height = int(timeline["height"])
    frame_count = int(timeline["frame_count"])
    shots = timeline["shots"]
    subtitles = timeline["subtitles"]
    render_config = style.get("render", {})
    palette_image = _palette_image(style)
    contexts = [_shot_context(shot, style) for shot in shots]
    bindings = build_frame_bindings(timeline)

    font_path = find_font(style) if subtitles else None
    if font_path is not None:
        for context in contexts:
            context["font_path"] = str(font_path)
    prepared_subtitles = {
        subtitle["id"]: _prepare_subtitle(subtitle, style, str(font_path), width, height)
        for subtitle in subtitles
    }
    requests = _keyframe_requests(shots, keyframes)
    keyframe_files: list[str] = []
    black_threshold = float(style.get("qc", {}).get("black_frame_mean_luma", 8.0))
    subtitle_config = style.get("subtitle", {})
    default_contrast = _contrast(
        _rgb(subtitle_config.get("background", "cream"), style.get("palette", {})),
        _rgb(subtitle_config.get("text_color", "ink"), style.get("palette", {})),
    )

    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        str(render_config.get("preset", "medium")),
        "-crf",
        str(render_config.get("crf", 14)),
        "-pix_fmt",
        str(render_config.get("pixel_format", "yuv420p")),
        str(output),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError("无法启动 FFmpeg，请确认 ffmpeg 已安装并位于 PATH") from exc
    assert process.stdin is not None
    assert process.stderr is not None

    def ffmpeg_error() -> str:
        return process.stderr.read().decode("utf-8", errors="replace").strip()

    current_shot_index = -1
    previous_shot_image: Image.Image | None = None
    last_raw_image: Image.Image | None = None
    last_low_array: np.ndarray | None = None
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
            for frame in range(frame_count):
                binding = bindings[frame]
                shot_index = int(binding["shot_index"])
                shot = shots[shot_index]
                if shot_index != current_shot_index:
                    previous_shot_image = last_raw_image
                    current_shot_index = shot_index
                local_frame = int(binding["local_frame"])
                raw_low = _render_low_frame(shot, local_frame, style, contexts[shot_index])
                transition_name = str(shot.get("transition_in", "hard_cut"))
                transition_frames = max(0, int(shot.get("transition_frames", 0)))
                transition_out_name = str(shots[-1].get("transition_out", "hard_cut"))
                transition_out_frames = max(0, int(shots[-1].get("transition_out_frames", 0)))
                transition_out_start = frame_count - transition_out_frames
                active_transition = None
                low = raw_low
                if local_frame < transition_frames and transition_name != "hard_cut":
                    if previous_shot_image is None:
                        previous_shot_image = Image.new("RGB", raw_low.size, (0, 0, 0))
                    low = _transition(
                        raw_low,
                        previous_shot_image,
                        transition_name,
                        (local_frame + 1) / max(1, transition_frames),
                        palette_image,
                    )
                    active_transition = transition_name
                elif local_frame == 0:
                    active_transition = "hard_cut"
                if frame >= transition_out_start and transition_out_frames > 0 and transition_out_name != "hard_cut":
                    low = _transition(
                        low,
                        Image.new("RGB", raw_low.size, (0, 0, 0)),
                        transition_out_name,
                        (frame - transition_out_start + 1) / transition_out_frames,
                        palette_image,
                    )
                    active_transition = transition_out_name
                last_raw_image = raw_low

                high = low.resize((width, height), Image.Resampling.NEAREST)
                subtitle_id = binding["subtitle_id"]
                subtitle_active = subtitle_id is not None
                overflow = False
                contrast = default_contrast
                if subtitle_active:
                    prepared = prepared_subtitles[subtitle_id]
                    _draw_subtitle(high, prepared)
                    overflow = bool(prepared["overflow"])
                    contrast = float(prepared["contrast"])

                low_array = np.asarray(low, dtype=np.uint8)
                frame_difference = (
                    0.0
                    if last_low_array is None
                    else float(
                        np.mean(
                            np.abs(
                                low_array.astype(np.int16)
                                - last_low_array.astype(np.int16)
                            )
                        )
                    )
                )
                last_low_array = low_array
                mean_luma = float(
                    np.mean(
                        low_array[..., 0].astype(np.float32) * 0.2126
                        + low_array[..., 1].astype(np.float32) * 0.7152
                        + low_array[..., 2].astype(np.float32) * 0.0722
                    )
                )
                packed = (
                    (low_array[..., 0].astype(np.uint32) << 16)
                    | (low_array[..., 1].astype(np.uint32) << 8)
                    | low_array[..., 2].astype(np.uint32)
                )
                palette_size = int(np.unique(packed).size)
                record = {
                    "frame": frame,
                    "pts": frame / fps,
                    "shot_index": shot_index,
                    "shot_id": shot["id"],
                    "local_frame": local_frame,
                    "scene": shot.get("scene", "generic"),
                    "visual_line_id": binding["visual_line_id"],
                    "lyric_cue_id": binding["lyric_cue_id"],
                    "lyric_line_id": binding["lyric_line_id"],
                    "subtitle_id": subtitle_id,
                    "weather": binding["weather"],
                    "weather_from": binding["weather_from"],
                    "weather_transition": binding["weather_transition"],
                    "weather_transition_stage": binding["weather_transition_stage"],
                    "weather_transition_steps": binding["weather_transition_steps"],
                    "weather_mix": binding["weather_mix"],
                    "transition": active_transition,
                    "mean_luma": round(mean_luma, 6),
                    "palette_size": palette_size,
                    "frame_difference": round(frame_difference, 6),
                    "subtitle_active": subtitle_active,
                    "subtitle_overflow": overflow,
                    "contrast": round(float(contrast), 6),
                    "source_black": mean_luma < black_threshold,
                }
                log_handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

                for keyframe_path in requests.get(frame, []):
                    high.save(keyframe_path, format="PNG")
                    keyframe_files.append(str(keyframe_path))
                try:
                    process.stdin.write(high.tobytes())
                except OSError as exc:
                    stderr = ffmpeg_error()
                    process.wait()
                    raise RuntimeError(f"FFmpeg 提前关闭视频管道（BrokenPipe/OSError）：{stderr or '无 stderr 信息'}") from exc
        try:
            process.stdin.close()
        except OSError as exc:
            stderr = ffmpeg_error()
            process.wait()
            raise RuntimeError(f"FFmpeg 在关闭输入时报告 BrokenPipe/OSError：{stderr or '无 stderr 信息'}") from exc
        stderr = ffmpeg_error()
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"FFmpeg 编码失败（退出码 {return_code}）：{stderr or '无 stderr 信息'}")
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        for stream in (process.stdin, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    return {
        "output_path": str(output),
        "frames_log_path": str(log_path),
        "frame_count": frame_count,
        "fps": fps,
        "width": width,
        "height": height,
        "duration": frame_count / fps,
        "keyframe_files": keyframe_files,
    }
