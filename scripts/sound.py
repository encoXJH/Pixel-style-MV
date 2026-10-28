from __future__ import annotations

import hashlib
import json
import math
import re
import wave
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["create_sound_design"]

_PEAK_CEILING = 0.75
_MAX_CUES_PER_SHOT = 4
_AMBIENT_DURATIONS = {
    "rain": 6.0,
    "wind": 6.0,
    "waves": 6.0,
    "train": 5.0,
    "room_tone": 6.0,
}
_ONE_SHOT_DURATIONS = {
    "footsteps": 1.8,
    "bicycle_bell": 0.65,
    "page_turn": 0.45,
    "camera_shutter": 0.2,
    "fireworks": 1.2,
    "transition": 0.35,
}
_RELATIVE_GAINS = {
    "rain": 0.55,
    "wind": 0.5,
    "waves": 0.6,
    "train": 0.65,
    "room_tone": 0.35,
    "footsteps": 0.7,
    "bicycle_bell": 0.65,
    "page_turn": 0.55,
    "camera_shutter": 0.5,
    "fireworks": 0.85,
    "transition": 0.45,
}
_ALIASES = (
    ("rain", ("rain", "rainy", "雨")),
    ("wind", ("wind", "windy", "breeze", "snow", "snowy", "风", "雪")),
    ("waves", ("wave", "waves", "ocean", "sea", "seaside", "surf", "海浪", "海边", "大海")),
    ("train", ("train", "rail", "railway", "列车", "火车")),
    ("footsteps", ("footstep", "footsteps", "walk", "walking", "脚步", "走路", "行走")),
    ("bicycle_bell", ("bicycle_bell", "bike_bell", "bicycle", "bike", "单车", "自行车", "车铃")),
    ("page_turn", ("page", "paper", "book", "翻书", "翻页", "书页", "纸张")),
    ("camera_shutter", ("shutter", "camera", "相机", "快门", "拍照")),
    ("fireworks", ("firework", "fireworks", "烟花", "烟火")),
    ("room_tone", ("room_tone", "room tone", "stage", "舞台", "房间")),
    ("transition", ("whoosh", "wipe", "iris", "dissolve", "white_flash", "转场")),
)


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数值")
    return result


def _integer(value: Any, name: str) -> int:
    result = _number(value, name)
    if result != math.floor(result):
        raise ValueError(f"{name} 必须是整数")
    return int(result)


def _timing(timeline: dict[str, Any]) -> tuple[float, int, float]:
    if not isinstance(timeline, dict):
        raise ValueError("timeline 必须是对象")
    fps = _number(timeline.get("fps"), "timeline.fps")
    frame_count = _integer(timeline.get("frame_count"), "timeline.frame_count")
    source_duration = _number(timeline.get("duration"), "timeline.duration")
    if fps <= 0 or frame_count <= 0 or source_duration <= 0:
        raise ValueError("timeline 的 duration、frame_count、fps 必须为正数")
    if frame_count != int(math.ceil(source_duration * fps)):
        raise ValueError("timeline.frame_count 必须等于 ceil(duration × fps)")
    return frame_count / fps, frame_count, fps


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, dict):
        if value.get("enabled") is False:
            return []
        result: list[str] = []
        for key in ("type", "name", "id", "effect", "sfx"):
            if key in value:
                result.extend(_strings(value[key]))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_strings(item))
        return result
    return []


def _matches_alias(text: str, alias: str) -> bool:
    if not alias.isascii():
        return alias in text
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None


def _requested_kinds(shot: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("weather", "scene", "action", "effects", "sfx"):
        values.extend(_strings(shot.get(key)))
    for key in ("transition", "transition_in", "transition_out"):
        value = shot.get(key)
        for text in _strings(value):
            if text not in ("hard_cut", "none", "cut"):
                values.append(text)
    kinds: list[str] = []
    for text in values:
        normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
        for kind, aliases in _ALIASES:
            if kind not in kinds and any(_matches_alias(normalized, alias) for alias in aliases):
                kinds.append(kind)
    return kinds[:_MAX_CUES_PER_SHOT]


def _rng(timeline: dict[str, Any], shot: dict[str, Any], index: int, kind: str) -> np.random.Generator:
    shot_id = str(shot.get("id", f"shot_{index:04d}"))
    seed = shot.get("seed", timeline.get("seed", 0))
    material = f"{seed}|{shot_id}|{index}|{kind}".encode("utf-8")
    digest = hashlib.blake2b(material, digest_size=8).digest()
    return np.random.default_rng(int.from_bytes(digest, "little"))


def _unit(signal: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(signal))) if signal.size else 0.0
    if peak > 0 and math.isfinite(peak):
        return signal / peak
    return np.zeros_like(signal)


def _colored_noise(
    count: int,
    sample_rate: int,
    rng: np.random.Generator,
    low: float,
    high: float,
) -> np.ndarray:
    noise = rng.standard_normal(count)
    if count < 16:
        return _unit(noise)
    frequencies = np.fft.rfftfreq(count, 1.0 / sample_rate)
    response = (frequencies / (frequencies + max(1.0, low))) / (
        1.0 + (frequencies / max(low + 1.0, high)) ** 2
    )
    response[0] = 0.0
    colored = np.fft.irfft(np.fft.rfft(noise) * response, n=count)
    return _unit(colored)


def _pan(mono: np.ndarray, pan: float) -> np.ndarray:
    angle = (max(-1.0, min(1.0, pan)) + 1.0) * math.pi / 4.0
    return np.column_stack((mono * math.cos(angle), mono * math.sin(angle)))


def _fade(signal: np.ndarray, sample_rate: int, seconds: float) -> np.ndarray:
    fade = min(int(round(seconds * sample_rate)), max(0, (len(signal) - 1) // 2))
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float64)
        signal[:fade] *= ramp[:, None]
        signal[-fade:] *= ramp[::-1, None]
    return signal


def _render_cue(
    kind: str,
    count: int,
    sample_rate: int,
    rng: np.random.Generator,
    fade_seconds: float,
) -> np.ndarray:
    t = np.arange(count, dtype=np.float64) / sample_rate
    duration = count / sample_rate
    ambient = kind in _AMBIENT_DURATIONS

    if kind in ("rain", "wind", "waves"):
        if kind == "rain":
            bands = (700.0, 9000.0)
        elif kind == "wind":
            bands = (45.0, 750.0)
        else:
            bands = (55.0, 1500.0)
        channels = []
        for _ in range(2):
            noise = _colored_noise(count, sample_rate, rng, *bands)
            if kind == "rain":
                modulation = 0.82 + 0.18 * np.sin(2 * np.pi * 0.7 * t + rng.uniform(0, 2 * np.pi))
            elif kind == "wind":
                modulation = 0.5 + 0.5 * np.sin(2 * np.pi * 0.16 * t + rng.uniform(0, 2 * np.pi)) ** 2
            else:
                modulation = 0.2 + 0.8 * np.sin(2 * np.pi * 0.18 * t + rng.uniform(0, 2 * np.pi)) ** 2
            channels.append(noise * modulation)
        signal = np.column_stack(channels)
    elif kind == "train":
        mono = 0.5 * np.sin(2 * np.pi * 46 * t) + 0.25 * np.sin(2 * np.pi * 92 * t)
        mono += 0.45 * _colored_noise(count, sample_rate, rng, 35.0, 500.0)
        for at in np.arange(0.12, duration, 0.48):
            start = int(at * sample_rate)
            length = min(int(0.09 * sample_rate), count - start)
            if length > 0:
                local = np.arange(length) / sample_rate
                mono[start : start + length] += np.exp(-local * 45) * np.sin(2 * np.pi * 170 * local)
        signal = _pan(mono, -0.1)
    elif kind == "room_tone":
        mono = 0.55 * np.sin(2 * np.pi * 60 * t) + 0.2 * np.sin(2 * np.pi * 120 * t)
        mono += 0.25 * _colored_noise(count, sample_rate, rng, 80.0, 900.0)
        signal = _pan(mono, 0.0)
    elif kind == "footsteps":
        signal = np.zeros((count, 2), dtype=np.float64)
        for step, at in enumerate(np.arange(0.04, duration, 0.31)):
            start = int(at * sample_rate)
            length = min(int(0.13 * sample_rate), count - start)
            if length > 0:
                local = np.arange(length) / sample_rate
                thud = np.exp(-local * 30) * (
                    np.sin(2 * np.pi * rng.uniform(75, 105) * local)
                    + 0.3 * rng.standard_normal(length)
                )
                signal[start : start + length] += _pan(thud, -0.35 if step % 2 else 0.35)
    elif kind == "bicycle_bell":
        mono = np.exp(-t * 7.5) * (
            np.sin(2 * np.pi * 1760 * t) + 0.55 * np.sin(2 * np.pi * 2349 * t)
        )
        delayed = np.maximum(0.0, t - 0.18)
        mono += (t >= 0.18) * 0.55 * np.exp(-delayed * 9) * np.sin(2 * np.pi * 1760 * delayed)
        signal = _pan(mono, 0.2)
    elif kind == "page_turn":
        noise = _colored_noise(count, sample_rate, rng, 500.0, 7000.0)
        envelope = np.sin(np.pi * np.minimum(1.0, t / duration)) ** 1.5
        signal = _pan(noise * envelope * (0.7 + 0.3 * np.sin(2 * np.pi * 19 * t) ** 2), -0.15)
    elif kind == "camera_shutter":
        mono = np.zeros(count, dtype=np.float64)
        for at in (0.0, 0.075):
            start = int(at * sample_rate)
            length = min(int(0.045 * sample_rate), count - start)
            if length > 0:
                local = np.arange(length) / sample_rate
                click = np.exp(-local * 90) * (rng.standard_normal(length) + np.sin(2 * np.pi * 1300 * local))
                mono[start : start + length] += click
        signal = _pan(mono, 0.0)
    elif kind == "fireworks":
        mono = np.exp(-t * 3.8) * np.sin(2 * np.pi * (72 * t - 14 * t * t))
        mono += 0.25 * _colored_noise(count, sample_rate, rng, 100.0, 2500.0) * np.exp(-t * 4)
        for _ in range(10):
            start = int(rng.uniform(0.08, max(0.09, duration * 0.8)) * sample_rate)
            length = min(int(0.035 * sample_rate), count - start)
            if length > 0:
                local = np.arange(length) / sample_rate
                mono[start : start + length] += 0.5 * np.exp(-local * 75) * rng.standard_normal(length)
        signal = _pan(mono, rng.uniform(-0.3, 0.3))
    else:
        noise = _colored_noise(count, sample_rate, rng, 250.0, 5000.0)
        envelope = np.sin(np.pi * np.minimum(1.0, t / duration)) ** 2
        signal = _pan(noise * envelope, 0.0)

    signal = _unit(np.nan_to_num(signal, nan=0.0, posinf=0.0, neginf=0.0))
    return _fade(signal, sample_rate, fade_seconds if ambient else min(fade_seconds, 0.01))


def _write_outputs(output: Path, sample_rate: int, samples: np.ndarray, manifest: dict[str, Any]) -> None:
    pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(output / "sound-cues.wav"), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    peak = float(np.max(np.abs(pcm.astype(np.int32)))) / 32767.0 if pcm.size else 0.0
    manifest["peak"] = round(peak, 8)
    (output / "sound-design.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def create_sound_design(timeline: dict, style: dict, output_dir: str | Path) -> dict:
    """Generate original, deterministic programmatic cues; never recreates a reference mix."""
    frame_duration, frame_count, fps = _timing(timeline)
    config = style.get("sound", {}) if isinstance(style, dict) else {}
    if not isinstance(config, dict):
        raise ValueError("style.sound 必须是对象")
    enabled = bool(config.get("enabled", True))
    sample_rate = _integer(config.get("sample_rate", 48000), "style.sound.sample_rate")
    if not 8000 <= sample_rate <= 192000:
        raise ValueError("style.sound.sample_rate 必须在 8000–192000 之间")
    cue_gain = _number(config.get("cue_gain", 0.16), "style.sound.cue_gain")
    max_cue_gain = _number(config.get("max_cue_gain", 0.18), "style.sound.max_cue_gain")
    fade_seconds = _number(config.get("fade_seconds", 0.12), "style.sound.fade_seconds")
    if cue_gain < 0 or max_cue_gain < 0 or fade_seconds < 0:
        raise ValueError("sound 的 cue_gain、max_cue_gain、fade_seconds 不能为负数")

    sample_count = max(1, int(round(frame_duration * sample_rate)))
    duration = sample_count / sample_rate
    mix = np.zeros((sample_count, 2), dtype=np.float32)
    cues: list[dict[str, Any]] = []
    shots = timeline.get("shots", [])
    if not isinstance(shots, list):
        raise ValueError("timeline.shots 必须是数组")

    if enabled and cue_gain > 0 and max_cue_gain > 0:
        for index, shot in enumerate(shots):
            if not isinstance(shot, dict):
                raise ValueError(f"shot {index} 必须是对象")
            start_frame = _integer(shot.get("start_frame"), f"shot {index}.start_frame")
            end_frame = _integer(shot.get("end_frame"), f"shot {index}.end_frame")
            if start_frame < 0 or end_frame > frame_count or end_frame <= start_frame:
                raise ValueError(f"shot {index} 帧边界无效")
            shot_id = str(shot.get("id", f"shot_{index:04d}"))
            shot_start = start_frame / fps
            shot_end = min(duration, end_frame / fps)
            for kind in _requested_kinds(shot):
                available = shot_end - shot_start
                if available <= 0:
                    continue
                limit = (
                    _AMBIENT_DURATIONS[kind]
                    if kind in _AMBIENT_DURATIONS
                    else _ONE_SHOT_DURATIONS[kind]
                )
                start_sample = int(round(shot_start * sample_rate))
                count = min(int(round(min(available, limit) * sample_rate)), sample_count - start_sample)
                if count < 2:
                    continue
                end_sample = start_sample + count
                gain = min(cue_gain * _RELATIVE_GAINS[kind], max_cue_gain)
                signal = _render_cue(kind, count, sample_rate, _rng(timeline, shot, index, kind), fade_seconds)
                mix[start_sample:end_sample] += (signal * gain).astype(np.float32)
                cues.append(
                    {
                        "id": f"cue_{len(cues):04d}",
                        "shot_id": shot_id,
                        "type": kind,
                        "start": start_sample / sample_rate,
                        "end": end_sample / sample_rate,
                        "gain": gain,
                    }
                )

    if mix.size and not np.isfinite(mix).all():
        raise RuntimeError("程序化声音合成产生了非有限采样")
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > _PEAK_CEILING:
        scale = _PEAK_CEILING / peak
        mix *= scale
        for cue in cues:
            cue["gain"] *= scale

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "enabled": enabled,
        "original_reference_audio_available": False,
        "music_source": "user_song",
        "design_status": "original_programmatic_cues",
        "sample_rate": sample_rate,
        "channels": 2,
        "duration": duration,
        "cue_count": len(cues),
        "cues": cues,
        "peak": 0.0,
        "file": "sound-cues.wav",
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _write_outputs(output, sample_rate, mix, manifest)
    return manifest
