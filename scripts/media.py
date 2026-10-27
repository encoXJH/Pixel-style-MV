from __future__ import annotations

import importlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np


_SAMPLE_RATE = 16000
_FRAME_LENGTH = 2048
_HOP_LENGTH = 512
_TIME_TOLERANCE = 0.05
_MIN_ALIGNMENT_CONFIDENCE = 0.35
_MODEL_CACHE = Path(__file__).resolve().parent.parent / ".models"
_LRC_TIMESTAMP = re.compile(
    r"\[(?:(?:\d{1,3}:)?[0-5]?\d:[0-5]\d)(?:[.:]\d{1,3})?\]"
)


def parse_lyrics(value: str | os.PathLike[str]) -> list[str]:
    """读取 UTF-8 歌词文件或文本，清理空行与常见 LRC 时间标签。"""
    if value is None:
        raise ValueError("歌词不能为空")

    if isinstance(value, os.PathLike):
        text = Path(value).read_text(encoding="utf-8-sig")
    else:
        raw = str(value)
        text = raw
        if "\n" not in raw and "\r" not in raw:
            try:
                candidate = Path(raw).expanduser()
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8-sig")
                elif candidate.is_dir():
                    raise ValueError(f"歌词路径是目录：{candidate}")
            except OSError:
                # 普通长歌词可能不是合法路径，仍按文本处理。
                text = raw

    lines: list[str] = []
    for raw_line in text.lstrip("\ufeff").splitlines():
        line = _LRC_TIMESTAMP.sub("", raw_line).strip()
        if line:
            lines.append(line)
    if not lines:
        raise ValueError("歌词为空或清理时间标签后没有有效文字")
    return lines


def _path_text(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


def _error_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return " ".join((value or "").split())[:600]


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _frame_rate(value: Any) -> float:
    text = str(value or "0/0")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return 0.0
            rate = float(numerator) / denominator_value
        else:
            rate = float(text)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return rate if math.isfinite(rate) and rate > 0 else 0.0


def probe_media(path: str | os.PathLike[str]) -> dict[str, Any]:
    """用 FFprobe 返回媒体时长、视频尺寸/帧率与音频信息。"""
    media_path = _path_text(path)
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        media_path,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 FFprobe，请先将 FFmpeg 工具加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"FFprobe 探测超时：{media_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 FFprobe：{exc}") from exc

    if completed.returncode != 0:
        detail = _error_text(completed.stderr) or "文件损坏、格式不受支持或没有可读取的流"
        raise RuntimeError(f"FFprobe 无法读取媒体（{media_path}）：{detail}")
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"FFprobe 返回了无效信息：{media_path}") from exc

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    durations = [_finite_number((payload.get("format") or {}).get("duration"))]
    durations.extend(_finite_number(stream.get("duration")) for stream in streams)
    duration = max(durations, default=0.0)
    if duration <= 0:
        raise RuntimeError(f"媒体时长无效或文件已损坏：{media_path}")

    fps = 0.0
    width = height = 0
    video_codec = None
    if video:
        fps = _frame_rate(video.get("avg_frame_rate")) or _frame_rate(video.get("r_frame_rate"))
        width = int(_finite_number(video.get("width")))
        height = int(_finite_number(video.get("height")))
        video_codec = video.get("codec_name")

    sample_rate = channels = 0
    audio_codec = None
    if audio:
        sample_rate = int(_finite_number(audio.get("sample_rate")))
        channels = int(_finite_number(audio.get("channels")))
        audio_codec = audio.get("codec_name")

    return {
        "duration": float(duration),
        "width": width,
        "height": height,
        "fps": float(fps),
        "audio": audio is not None,
        "sample_rate": sample_rate,
        "channels": channels,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
    }


def decode_pcm(
    path: str | os.PathLike[str],
    sample_rate: int = _SAMPLE_RATE,
    max_seconds: float | None = None,
) -> np.ndarray:
    """用 FFmpeg 解码为 float32 单声道 PCM。"""
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer)):
        raise ValueError("sample_rate 必须是正整数")
    if int(sample_rate) <= 0:
        raise ValueError("sample_rate 必须是正整数")
    if max_seconds is not None:
        max_seconds = _finite_number(max_seconds, -1.0)
        if max_seconds <= 0:
            raise ValueError("max_seconds 必须是正数或 None")

    media_path = _path_text(path)
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", media_path]
    if max_seconds is not None:
        command.extend(["-t", repr(float(max_seconds))])
    command.extend(
        [
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(int(sample_rate)),
            "-f",
            "f32le",
            "pipe:1",
        ]
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            shell=False,
            timeout=180,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 FFmpeg，请先将 FFmpeg 工具加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"FFmpeg 解码超时：{media_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 FFmpeg：{exc}") from exc

    if completed.returncode != 0:
        detail = _error_text(completed.stderr) or "文件损坏或没有可解码的音轨"
        raise RuntimeError(f"FFmpeg 无法解码音频（{media_path}）：{detail}")
    if not completed.stdout:
        raise RuntimeError(f"音频解码结果为空：{media_path}")
    if len(completed.stdout) % 4:
        raise RuntimeError(f"FFmpeg 返回了不完整的 PCM 数据：{media_path}")

    pcm = np.frombuffer(completed.stdout, dtype="<f4").astype(np.float32, copy=True)
    if pcm.size == 0:
        raise RuntimeError(f"音频没有可用采样：{media_path}")
    if not np.isfinite(pcm).all():
        np.nan_to_num(pcm, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    return pcm


def _rms_track(pcm: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    pad = _FRAME_LENGTH // 2
    padded = np.pad(pcm, (pad, pad))
    count = max(1, 1 + pcm.size // _HOP_LENGTH)
    windows = np.lib.stride_tricks.sliding_window_view(padded, _FRAME_LENGTH)
    windows = windows[::_HOP_LENGTH][:count]
    rms = np.sqrt(np.mean(np.square(windows), axis=1, dtype=np.float64))
    times = np.arange(count, dtype=np.float64) * (_HOP_LENGTH / sample_rate)
    return times, rms


def _float_seconds(values: Any, duration: float) -> list[float]:
    try:
        array = np.asarray(values, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return []
    array = array[np.isfinite(array) & (array >= 0) & (array <= duration + 1e-6)]
    return [float(min(duration, max(0.0, value))) for value in array]


def _section_times(
    rms_times: np.ndarray,
    rms: np.ndarray,
    duration: float,
    tempo: float,
) -> list[float]:
    """按平滑能量突变给出候选段落边界，始终包含首尾。"""
    sections = [0.0]
    if rms.size >= 5 and rms_times.size == rms.size and duration > 4:
        intervals = np.diff(rms_times)
        intervals = intervals[np.isfinite(intervals) & (intervals > 0)]
        frame_rate = 1.0 / float(np.median(intervals)) if intervals.size else 0.0
        window = max(1, int(round(frame_rate * 2.0))) if frame_rate else 1
        window = min(window, int(rms.size))
        smooth = (
            np.convolve(rms, np.ones(window) / window, mode="same")
            if window > 1
            else rms.astype(np.float64, copy=False)
        )
        changes = np.abs(np.diff(smooth, prepend=smooth[0]))
        candidates = np.flatnonzero(
            (changes >= np.concatenate(([changes[0]], changes[:-1])))
            & (changes >= np.concatenate((changes[1:], [changes[-1]])))
        )
        min_gap = max(4.0, min(16.0, (60.0 / tempo) * 16 if tempo > 0 else 8.0))
        candidates = [
            int(index)
            for index in candidates
            if min_gap < rms_times[index] < duration - min_gap and changes[index] > 0
        ]
        if candidates:
            threshold = float(np.percentile(changes[candidates], 60))
            candidates = [index for index in candidates if changes[index] >= threshold]
            candidates.sort(key=lambda index: float(changes[index]), reverse=True)
            for index in candidates:
                second = float(rms_times[index])
                if all(abs(second - existing) >= min_gap for existing in sections):
                    sections.append(second)
                if len(sections) >= 12:
                    break
    sections.append(float(duration))
    return [float(value) for value in sorted(set(sections))]


def _fallback_analysis(
    pcm: np.ndarray,
    sample_rate: int,
    duration: float,
    rms_times: np.ndarray,
    rms: np.ndarray,
) -> tuple[float, list[float], list[float]]:
    envelope = np.maximum(np.diff(rms, prepend=rms[0]), 0.0)
    if envelope.size == 0 or float(np.max(envelope)) <= 1e-10:
        return 0.0, [], []

    onset_indices: list[int] = []
    if envelope.size >= 3:
        threshold = float(np.median(envelope) + 0.5 * np.std(envelope))
        peaks = np.flatnonzero(
            (envelope[1:-1] > envelope[:-2])
            & (envelope[1:-1] >= envelope[2:])
            & (envelope[1:-1] > threshold)
        )
        onset_indices = [int(index + 1) for index in peaks]
    onsets = [float(rms_times[index]) for index in onset_indices]

    centered = envelope.astype(np.float64) - float(np.mean(envelope))
    size = centered.size
    nfft = 1 << (2 * size - 1).bit_length()
    spectrum = np.fft.rfft(centered, nfft)
    correlation = np.fft.irfft(spectrum * np.conj(spectrum), nfft)[:size]
    zero_lag = float(correlation[0])
    if zero_lag > 1e-12:
        correlation /= zero_lag

    frame_rate = sample_rate / _HOP_LENGTH
    min_lag = max(1, int(math.floor(frame_rate * 60.0 / 240.0)))
    max_lag = min(size - 1, int(math.ceil(frame_rate * 60.0 / 40.0)))
    tempo = 0.0
    if max_lag >= min_lag and zero_lag > 1e-12:
        candidates = correlation[min_lag : max_lag + 1]
        if candidates.size and np.isfinite(candidates).all():
            best_lag = min_lag + int(np.argmax(candidates))
            best_score = float(correlation[best_lag])
            if best_score > 1e-8:
                local_peaks = np.flatnonzero(
                    (candidates[1:-1] >= candidates[:-2])
                    & (candidates[1:-1] > candidates[2:])
                ) + 1
                strong_peaks = local_peaks[candidates[local_peaks] >= best_score * 0.8]
                if strong_peaks.size:
                    best_lag = min_lag + int(strong_peaks[0])
                tempo = float(60.0 * frame_rate / best_lag)

    beats: list[float] = []
    if tempo > 0:
        period = 60.0 / tempo
        phase = (onsets[0] % period) if onsets else 0.0
        beats = [float(value) for value in np.arange(phase, duration + 1e-9, period)]
        beats = [value for value in beats if 0 <= value <= duration]
    return tempo, beats, onsets


def analyze_audio(audio_path: str | os.PathLike[str]) -> dict[str, Any]:
    """分析节拍、onset 与 RMS；librosa 故障时使用 NumPy 降级。"""
    pcm = decode_pcm(audio_path, sample_rate=_SAMPLE_RATE)
    duration = float(pcm.size / _SAMPLE_RATE)
    rms_times, rms = _rms_track(pcm, _SAMPLE_RATE)
    warnings: list[str] = []

    try:
        librosa = importlib.import_module("librosa")
        rms_values = librosa.feature.rms(
            y=pcm,
            frame_length=_FRAME_LENGTH,
            hop_length=_HOP_LENGTH,
        )[0]
        rms_times_values = librosa.times_like(
            rms_values,
            sr=_SAMPLE_RATE,
            hop_length=_HOP_LENGTH,
        )
        onset_envelope = librosa.onset.onset_strength(
            y=pcm,
            sr=_SAMPLE_RATE,
            hop_length=_HOP_LENGTH,
        )
        onsets_values = librosa.onset.onset_detect(
            onset_envelope=onset_envelope,
            sr=_SAMPLE_RATE,
            hop_length=_HOP_LENGTH,
            units="time",
        )
        tempo_value, beats_values = librosa.beat.beat_track(
            onset_envelope=onset_envelope,
            sr=_SAMPLE_RATE,
            hop_length=_HOP_LENGTH,
            units="time",
        )
        tempo_scalar = np.asarray(tempo_value).reshape(-1)[0]
        tempo = _finite_number(tempo_scalar)
        rms = np.asarray(rms_values, dtype=np.float64).reshape(-1)
        rms_times = np.asarray(rms_times_values, dtype=np.float64).reshape(-1)
        if rms.size == 0 or rms.size != rms_times.size:
            raise ValueError("librosa 返回了不匹配的 RMS 时间轴")
        beats = _float_seconds(beats_values, duration)
        onsets = _float_seconds(onsets_values, duration)
        backend = "librosa"
    except Exception as exc:
        detail = " ".join(str(exc).split())[:240]
        warnings.append(f"librosa 不可用或分析失败，已使用 NumPy 降级：{detail}")
        tempo, beats, onsets = _fallback_analysis(
            pcm,
            _SAMPLE_RATE,
            duration,
            rms_times,
            rms,
        )
        backend = "numpy"

    finite_rms = np.nan_to_num(rms.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    sections = _section_times(rms_times, finite_rms, duration, tempo)
    return {
        "duration": duration,
        "tempo": float(max(0.0, tempo)),
        "beats": beats,
        "onsets": onsets,
        "rms_times": [float(value) for value in rms_times],
        "rms": [float(max(0.0, value)) for value in finite_rms],
        "section_times": sections,
        "backend": backend,
        "warnings": warnings,
    }


def _object_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _unit_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(confidence):
        return None
    return float(min(1.0, max(0.0, confidence)))


def _segment_confidence(segment: Any) -> float:
    for key in ("confidence", "probability"):
        confidence = _unit_confidence(_object_value(segment, key))
        if confidence is not None:
            return confidence

    words = _object_value(segment, "words", []) or []
    word_values = []
    for word in words:
        for key in ("probability", "confidence"):
            confidence = _unit_confidence(_object_value(word, key))
            if confidence is not None:
                word_values.append(confidence)
                break
    if word_values:
        return float(sum(word_values) / len(word_values))

    avg_logprob = _finite_number(_object_value(segment, "avg_logprob"), float("nan"))
    no_speech = _unit_confidence(_object_value(segment, "no_speech_prob"))
    if math.isfinite(avg_logprob):
        confidence = math.exp(max(-50.0, min(0.0, avg_logprob)))
        if no_speech is not None:
            confidence *= 1.0 - no_speech
        return float(min(1.0, max(0.0, confidence)))
    if no_speech is not None:
        return 1.0 - no_speech
    return 0.0


def _aligned_lines(result: Any, texts: list[str], duration: float) -> list[dict[str, Any]]:
    segments = _object_value(result, "segments")
    if segments is None:
        raise ValueError("stable-ts 结果中没有 segments")
    segments = list(segments)
    if not segments:
        raise ValueError("stable-ts 未检测到可用语音句段")
    if len(segments) != len(texts):
        raise ValueError(
            f"original_split 返回 {len(segments)} 段，无法一一映射到 {len(texts)} 行用户歌词"
        )

    lines: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, (text, segment) in enumerate(zip(texts, segments)):
        start = _finite_number(_object_value(segment, "start"), float("nan"))
        end = _finite_number(_object_value(segment, "end"), float("nan"))
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"第 {index + 1} 行对齐时间不是有限数值")
        if start < -_TIME_TOLERANCE or end > duration + _TIME_TOLERANCE or end <= start:
            raise ValueError(f"第 {index + 1} 行对齐时间越界或句长无效")

        start = min(duration, max(0.0, start))
        end = min(duration, max(0.0, end))
        if start < previous_end:
            if previous_end - start > _TIME_TOLERANCE:
                raise ValueError(f"第 {index + 1} 行对齐时间发生重叠或非单调")
            start = previous_end
        if end <= start or (lines and end <= lines[-1]["end"]):
            raise ValueError(f"第 {index + 1} 行对齐时间非严格单调")

        lines.append(
            {
                "id": index,
                "text": text,
                "start": float(start),
                "end": float(end),
                "status": "aligned",
                "confidence": _segment_confidence(segment),
            }
        )
        previous_end = end
    return lines


def _lyric_language(texts: list[str]) -> str:
    joined = "".join(texts)
    if re.search(r"[\u3040-\u30ff\uff66-\uff9f]", joined):
        return "ja"
    if re.search(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", joined):
        return "ko"
    if re.search(r"[\u3400-\u9fff]", joined):
        return "zh"
    return "unknown"


def _feature_rms(features: Any, duration: float) -> tuple[np.ndarray, np.ndarray]:
    if not isinstance(features, dict):
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    try:
        times = np.asarray(features.get("rms_times", []), dtype=np.float64).reshape(-1)
        rms = np.asarray(features.get("rms", []), dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    if not rms.size:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    if not times.size:
        times = np.arange(rms.size, dtype=np.float64) * (duration / rms.size)
    count = min(times.size, rms.size)
    times, rms = times[:count], rms[:count]
    valid = np.isfinite(times) & np.isfinite(rms) & (times >= 0) & (times <= duration)
    times, rms = times[valid], np.maximum(0.0, rms[valid])
    if times.size > 1 and np.any(np.diff(times) < 0):
        order = np.argsort(times)
        times, rms = times[order], rms[order]
    return times, rms


def _estimated_lines(
    texts: list[str],
    duration: float,
    features: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rms_times, rms = _feature_rms(features, duration)
    edge = min(0.75, duration * 0.03)
    region_start, region_end = edge, duration - edge
    if region_end <= region_start:
        region_start, region_end = 0.0, duration

    if rms.size and float(np.max(rms)) > 1e-10:
        active = np.flatnonzero(rms >= float(np.max(rms)) * 0.08)
        if active.size:
            candidate_start = max(region_start, float(rms_times[active[0]]) - 0.1)
            candidate_end = min(region_end, float(rms_times[active[-1]]) + 0.2)
            if candidate_end > candidate_start:
                region_start, region_end = candidate_start, candidate_end

    count = len(texts)
    region_duration = region_end - region_start
    if region_duration <= 0:
        raise ValueError("没有可用于歌词估计分配的时间区间")
    lengths = np.asarray(
        [max(1, len(re.sub(r"\s+", "", text))) for text in texts],
        dtype=np.float64,
    )
    floor = min(0.35, region_duration / (2.0 * count))
    durations = np.full(count, floor, dtype=np.float64)
    remaining = max(0.0, region_duration - float(np.sum(durations)))
    durations += remaining * lengths / float(np.sum(lengths))

    boundaries = [float(region_start)]
    elapsed = 0.0
    for value in durations[:-1]:
        elapsed += float(value)
        boundaries.append(float(region_start + elapsed))
    boundaries.append(float(region_end))

    lines = []
    for index, text in enumerate(texts):
        start, end = boundaries[index], boundaries[index + 1]
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + 1e-9):
            raise ValueError(f"第 {index + 1} 行估计时间无效")
        lines.append(
            {
                "id": index,
                "text": text,
                "start": start,
                "end": end,
                "status": "estimated",
                "confidence": 0.0,
            }
        )
    return lines


def _alignment_result(
    duration: float,
    language: str,
    backend: str,
    status: str,
    warnings: list[str],
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    confidence = (
        float(sum(line["confidence"] for line in lines) / len(lines))
        if lines
        else 0.0
    )
    return {
        "schema_version": 1,
        "duration": float(duration),
        "language": language,
        "backend": backend,
        "status": status,
        "confidence": confidence,
        "human_vocal_sync": "unverified",
        "warnings": warnings,
        "lines": lines,
    }


def align_lyrics(
    audio_path: str | os.PathLike[str],
    lyrics: list[str],
    duration: float,
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """优先 stable-ts 逐行对齐，失败时生成可出片的估计时间轴。"""
    if isinstance(lyrics, (str, bytes)) or not lyrics:
        raise ValueError("lyrics 必须是非空歌词字符串列表")
    texts = [str(text).strip() for text in lyrics]
    if any(not text for text in texts):
        raise ValueError("歌词列表不能包含空行")
    duration = _finite_number(duration)
    if duration <= 0:
        raise ValueError("duration 必须是有限正数")

    warnings: list[str] = []
    language = _lyric_language(texts)
    try:
        stable_whisper = importlib.import_module("stable_whisper")
        _MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        model = stable_whisper.load_faster_whisper(
            "small",
            device="cpu",
            compute_type="int8",
            download_root=str(_MODEL_CACHE),
        )
        if language == "unknown":
            _, info = model.transcribe_original(
                _path_text(audio_path),
                language=None,
                word_timestamps=False,
                without_timestamps=True,
                clip_timestamps=f"0,{min(30.0, duration):.6f}",
            )
            detected_language = _object_value(info, "language")
            if not isinstance(detected_language, str) or not detected_language.strip():
                raise ValueError("音频语言检测未返回有效语言")
            language = detected_language.strip()
        result = model.align(
            _path_text(audio_path),
            "\n".join(texts),
            language=language,
            original_split=True,
        )
        lines = _aligned_lines(result, texts, duration)
        result_language = _object_value(result, "language")
        if isinstance(result_language, str) and result_language.strip():
            language = result_language.strip()
        aligned = _alignment_result(
            duration,
            language,
            "stable-ts",
            "aligned",
            warnings,
            lines,
        )
        minimum_confidence = min(line["confidence"] for line in lines)
        if minimum_confidence < _MIN_ALIGNMENT_CONFIDENCE:
            for line in lines:
                line["status"] = "estimated"
            aligned["status"] = "estimated"
            aligned["backend"] = "stable-ts-low-confidence"
            aligned["warnings"].append(
                f"最低句级对齐置信度 {minimum_confidence:.3f} 缺失或低于 "
                f"{_MIN_ALIGNMENT_CONFIDENCE:.2f}；保留模型时间作为估计草稿，不声明精准人声同步"
            )
        return aligned
    except Exception as exc:
        detail = " ".join(str(exc).split())[:300]
        warnings.append(f"stable-ts 对齐不可用，已降级为估计时间轴：{detail}")

    # 词级 ASR 字符匹配回退未实现，避免把误识别文本冒充用户歌词。
    warnings.append("词级 ASR 回退未实现，本次直接使用 RMS/句长估计")
    if features is None:
        try:
            features = analyze_audio(audio_path)
            warnings.extend(features.get("warnings", []))
        except Exception as exc:
            detail = " ".join(str(exc).split())[:240]
            warnings.append(f"音频特征分析失败，改用纯句长估计：{detail}")
            features = None

    try:
        lines = _estimated_lines(texts, duration, features)
    except Exception as exc:
        detail = " ".join(str(exc).split())[:240]
        warnings.append(f"RMS 估计失败，尝试纯句长分配：{detail}")
        try:
            lines = _estimated_lines(texts, duration, None)
        except Exception as final_exc:
            final_detail = " ".join(str(final_exc).split())[:240]
            warnings.append(f"无法生成歌词时间轴：{final_detail}")
            return _alignment_result(
                duration,
                language,
                "none",
                "failed",
                warnings,
                [],
            )

    return _alignment_result(
        duration,
        language,
        "rms-length-estimator",
        "estimated",
        warnings,
        lines,
    )


def _numpy_cross_correlation(output: np.ndarray, source: np.ndarray) -> np.ndarray:
    size = output.size + source.size - 1
    nfft = 1 << (size - 1).bit_length()
    output_spectrum = np.fft.rfft(output, nfft)
    reversed_source_spectrum = np.fft.rfft(source[::-1], nfft)
    return np.fft.irfft(
        output_spectrum * reversed_source_spectrum,
        nfft,
    )[:size]


def measure_audio_offset(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    max_seconds: float | None = 90,
) -> dict[str, Any]:
    """测量成片相对源音频的延迟；正值表示成片音频滞后。"""
    source = decode_pcm(source_path, sample_rate=_SAMPLE_RATE, max_seconds=max_seconds)
    output = decode_pcm(output_path, sample_rate=_SAMPLE_RATE, max_seconds=max_seconds)

    source_level = float(np.sqrt(np.mean(np.square(source), dtype=np.float64)))
    output_level = float(np.sqrt(np.mean(np.square(output), dtype=np.float64)))
    if source_level < 1e-7 or output_level < 1e-7:
        return {
            "offset_ms": 0.0,
            "confidence": 0.0,
            "method": "cross-correlation",
            "warning": "源音频或成片音频为静音/近静音，无法可靠测量偏移",
        }

    source = source - np.mean(source, dtype=np.float64)
    output = output - np.mean(output, dtype=np.float64)
    source_std = float(np.sqrt(np.mean(np.square(source), dtype=np.float64)))
    output_std = float(np.sqrt(np.mean(np.square(output), dtype=np.float64)))
    if source_std < 1e-8 or output_std < 1e-8:
        return {
            "offset_ms": 0.0,
            "confidence": 0.0,
            "method": "cross-correlation",
            "warning": "音频去直流后为常量，无法可靠测量偏移",
        }
    source = source / source_std
    output = output / output_std

    try:
        signal = importlib.import_module("scipy.signal")
        correlation = signal.correlate(output, source, mode="full", method="fft")
        lags = signal.correlation_lags(output.size, source.size, mode="full")
        method = "scipy.signal.correlate(fft)"
    except Exception:
        correlation = _numpy_cross_correlation(output, source)
        lags = np.arange(-(source.size - 1), output.size, dtype=np.int64)
        method = "numpy.fft.cross-correlation"

    output_starts = np.maximum(lags, 0)
    source_starts = np.maximum(-lags, 0)
    overlaps = np.minimum(
        output.size - output_starts,
        source.size - source_starts,
    )
    min_size = min(output.size, source.size)
    min_overlap = min(min_size, max(_SAMPLE_RATE // 4, int(min_size * 0.1)))
    valid = overlaps >= min_overlap
    if not np.any(valid):
        return {
            "offset_ms": 0.0,
            "confidence": 0.0,
            "method": method,
            "warning": "两段音频没有足够的有效重叠，无法测量偏移",
        }

    source_energy = np.concatenate(
        ([0.0], np.cumsum(np.square(source), dtype=np.float64))
    )
    output_energy = np.concatenate(
        ([0.0], np.cumsum(np.square(output), dtype=np.float64))
    )
    output_ends = output_starts + overlaps
    source_ends = source_starts + overlaps
    norm = np.sqrt(
        (output_energy[output_ends] - output_energy[output_starts])
        * (source_energy[source_ends] - source_energy[source_starts])
    )
    usable = valid & np.isfinite(norm) & (norm > 1e-12)
    scores = np.full(correlation.shape, -np.inf, dtype=np.float64)
    scores[usable] = correlation[usable] / norm[usable]
    scores = np.clip(scores, -1.0, 1.0)
    if not np.any(usable) or not np.isfinite(scores[usable]).any():
        return {
            "offset_ms": 0.0,
            "confidence": 0.0,
            "method": method,
            "warning": "互相关没有有效峰值，无法测量偏移",
        }

    best_index = int(np.argmax(scores))
    best_score = float(scores[best_index])
    if not math.isfinite(best_score) or best_score <= 0:
        return {
            "offset_ms": 0.0,
            "confidence": 0.0,
            "method": method,
            "warning": "未找到有效的正相关峰，无法可靠测量偏移",
        }

    confidence = float(min(1.0, max(0.0, best_score)))
    warning = None
    if confidence < 0.5:
        warning = "互相关峰置信度较低，请人工复核音频偏移"
    return {
        "offset_ms": float(lags[best_index] * 1000.0 / _SAMPLE_RATE),
        "confidence": confidence,
        "method": method,
        "warning": warning,
    }


__all__ = [
    "parse_lyrics",
    "probe_media",
    "decode_pcm",
    "analyze_audio",
    "align_lyrics",
    "measure_audio_offset",
]
