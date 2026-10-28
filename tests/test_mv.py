from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import media
import mv
import preview
import qc
import render


def _write_pcm(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    pcm = np.clip(samples, -0.98, 0.98)
    pcm = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def _song(duration: float = 10.0, sample_rate: int = 22050) -> np.ndarray:
    t = np.arange(int(duration * sample_rate), dtype=np.float64) / sample_rate
    beat_phase = np.mod(t, 0.5)
    beat = np.exp(-beat_phase * 20.0) * np.sin(2 * np.pi * 90 * t)
    melody = np.sin(2 * np.pi * (220 + 20 * np.sin(2 * np.pi * 0.1 * t)) * t)
    harmony = np.sin(2 * np.pi * 440 * t + 0.3 * np.sin(2 * np.pi * 0.25 * t))
    return 0.24 * beat + 0.12 * melody + 0.06 * harmony


def _alignment(duration: float, status: str = "aligned") -> dict:
    times = [(0.4, 2.0), (2.2, 4.1), (4.4, 6.6), (6.8, min(duration - 0.5, 9.2))]
    if duration < 4:
        times = [(0.15, duration * 0.48), (duration * 0.52, duration - 0.15)]
    texts = ["教室里的那封信", "雨夜里等你回来", "海边日落和照片", "我们还唱着歌"][: len(times)]
    return {
        "schema_version": 1,
        "duration": duration,
        "language": "zh",
        "backend": "test-reference" if status == "aligned" else "test-estimator",
        "status": status,
        "confidence": 1.0 if status == "aligned" else 0.0,
        "human_vocal_sync": "unverified",
        "warnings": [],
        "lines": [
            {
                "id": index,
                "text": texts[index],
                "start": start,
                "end": end,
                "status": status,
                "confidence": 1.0 if status == "aligned" else 0.0,
            }
            for index, (start, end) in enumerate(times)
        ],
    }


class MediaAndTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.style = render.load_style()

    def test_parse_lyrics_text_and_lrc(self) -> None:
        lines = media.parse_lyrics("[00:01.00]第一句\n\n[00:02.50][00:03.00]第二句")
        self.assertEqual(lines, ["第一句", "第二句"])
        with self.assertRaises(ValueError):
            media.parse_lyrics(" \n[00:01.00] ")

    def test_estimated_fallback_is_explicit(self) -> None:
        original = media.importlib.import_module

        def without_stable(name: str):
            if name == "stable_whisper":
                raise ImportError("test: model unavailable")
            return original(name)

        features = {
            "rms_times": [0.0, 1.0, 2.0, 3.0],
            "rms": [0.2, 0.8, 0.6, 0.2],
        }
        with mock.patch("media.importlib.import_module", side_effect=without_stable):
            result = media.align_lyrics("unused.wav", ["第一句", "第二句"], 4.0, features)
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(len(result["lines"]), 2)
        self.assertEqual(result["human_vocal_sync"], "unverified")
        self.assertTrue(any("估计" in item for item in result["warnings"]))

    def test_any_low_confidence_line_is_a_draft(self) -> None:
        model = mock.Mock()
        model.transcribe_original.return_value = (iter([]), {"language": "en"})
        model.align.return_value = {
            "language": "en",
            "segments": [
                {"start": 0.2, "end": 1.5, "confidence": 0.95},
                {"start": 1.7, "end": 3.2, "confidence": 0.1},
            ],
        }
        stable = mock.Mock()
        stable.load_faster_whisper.return_value = model
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "media._MODEL_CACHE", Path(temporary) / "models"
        ), mock.patch("media.importlib.import_module", return_value=stable):
            result = media.align_lyrics("unused.wav", ["first line", "second line"], 4.0, {})
        self.assertGreater(result["confidence"], media._MIN_ALIGNMENT_CONFIDENCE)
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["backend"], "stable-ts-low-confidence")
        self.assertTrue(all(line["status"] == "estimated" for line in result["lines"]))
        self.assertEqual(model.align.call_args.kwargs["original_split"], True)
        self.assertEqual(model.align.call_args.kwargs["language"], "en")

    def test_latin_lyrics_use_detected_audio_language(self) -> None:
        model = mock.Mock()
        model.transcribe_original.return_value = (iter([]), {"language": "fr"})
        model.align.return_value = {
            "language": "fr",
            "segments": [{"start": 0.2, "end": 3.2, "confidence": 0.9}],
        }
        stable = mock.Mock()
        stable.load_faster_whisper.return_value = model
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "media._MODEL_CACHE", Path(temporary) / "models"
        ), mock.patch("media.importlib.import_module", return_value=stable):
            result = media.align_lyrics("unused.wav", ["la nuit"], 4.0, {})
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["language"], "fr")
        self.assertIsNone(model.transcribe_original.call_args.kwargs["language"])
        self.assertEqual(model.transcribe_original.call_args.kwargs["clip_timestamps"], "0,4.000000")
        self.assertEqual(model.align.call_args.kwargs["language"], "fr")

    def test_cjk_script_language_hints(self) -> None:
        self.assertEqual(media._lyric_language(["桜が咲く"]), "ja")
        self.assertEqual(media._lyric_language(["사랑해"]), "ko")
        self.assertEqual(media._lyric_language(["教室里的信"]), "zh")
        self.assertEqual(media._lyric_language(["la nuit"]), "unknown")

    def test_audio_offset_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_rate = 16000
            source = _song(2.0, sample_rate).astype(np.float32)
            delayed = np.pad(source, (1200, 0))[: source.size]
            _write_pcm(root / "source.wav", source, sample_rate)
            _write_pcm(root / "delayed.wav", delayed, sample_rate)
            result = media.measure_audio_offset(root / "source.wav", root / "delayed.wav")
            self.assertGreater(result["confidence"], 0.5)
            self.assertAlmostEqual(result["offset_ms"], 75.0, delta=2.0)

    def test_scene_matching_beat_snap_and_ending_iris(self) -> None:
        aligned = _alignment(4.0)
        aligned["lines"] = aligned["lines"][:2]
        aligned["lines"][0].update(text="教室里的信", start=0.0, end=1.8)
        aligned["lines"][1].update(text="雨夜红伞", start=1.8, end=3.8)
        features = {"beats": [1.82], "onsets": [], "rms_times": [0, 2, 4], "rms": [0.2, 0.8, 0.3]}
        timeline = render.plan_timeline(aligned, features, self.style)
        self.assertEqual(timeline["art_version"], 4)
        self.assertEqual(timeline["story_theme"], "campus")
        self.assertEqual(timeline["shots"][0]["scene"], "campus")
        self.assertEqual(timeline["shots"][1]["scene"], "rain_street")
        self.assertEqual(timeline["shots"][1]["weather"], "rain")
        self.assertEqual(timeline["shots"][1]["beat_snap_ms"], 0.0)
        self.assertFalse(timeline["shots"][1]["beat_snap_applied"])
        for cue in timeline["lyric_cues"]:
            subtitle = next(item for item in timeline["subtitles"] if item["cue_id"] == cue["id"])
            first_shot = next(shot for shot in timeline["shots"] if shot["id"] == cue["shot_ids"][0])
            self.assertEqual(cue["start_frame"], subtitle["start_frame"])
            self.assertEqual(cue["start_frame"], first_shot["start_frame"])
        last = timeline["shots"][-1]
        self.assertEqual(last["transition_out"], "iris_out")
        self.assertEqual(last["transition_out_frames"], 18)
        self.assertNotEqual(last["transition_in"], "iris_out")
        schedule = qc._transition_schedule(timeline, self.style, timeline["frame_count"])
        iris = next(item for item in schedule if item["type"] == "iris_out")
        self.assertEqual((iris["start"], iris["end"]), (timeline["frame_count"] - 18, timeline["frame_count"]))
        for subtitle in timeline["subtitles"]:
            self.assertLessEqual(abs(subtitle["error_start_ms"]), 50.0)
            self.assertEqual(subtitle["start_frame"], int(np.ceil(subtitle["target_start"] * 30)))

    def test_art_v4_story_arc_shot_types_and_loop(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        shots = timeline["shots"]
        first, last = shots[0], shots[-1]
        self.assertEqual(timeline["story_theme"], "station")
        self.assertEqual(first["scene"], last["scene"])
        self.assertEqual(first["weather"], last["weather"])
        self.assertEqual(first["story_phase"], "prologue")
        self.assertEqual(last["story_phase"], "epilogue")
        self.assertEqual(first["shot_type"], "landscape")
        self.assertEqual(last["shot_type"], "landscape")
        self.assertEqual(first["action"], "title_board")
        self.assertEqual(last["action"], "title_board")
        self.assertIsNone(first["compare_weather"])
        self.assertIsNone(last["compare_weather"])
        self.assertEqual(last["props"], [])
        self.assertFalse(first["characters"])
        self.assertEqual(shots[1]["compare_weather"], "rain")
        embrace = next(shot for shot in shots if shot["action"] == "embrace")
        self.assertEqual(embrace["characters"], ["protagonist", "companion"])
        heart = next(shot for shot in shots if shot["action"] == "heart_glass")
        self.assertEqual(heart["shot_type"], "object")
        self.assertEqual(heart["props"], [])
        self.assertTrue({shot["art_version"] for shot in shots} == {4})
        self.assertGreaterEqual(
            {shot["shot_type"] for shot in shots},
            {"landscape", "wide", "medium", "close", "object"},
        )
        self.assertGreaterEqual(
            {shot["scene"] for shot in shots},
            {"station", "seaside", "bedroom", "rain_street"},
        )
        object_shot = next(shot for shot in shots if shot["shot_type"] == "object")
        self.assertLessEqual(len(object_shot["props"]), 2)
        self.assertNotIn("cat", object_shot["props"])

    def test_art_v4_frames_have_motion_and_compact_subtitles(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        font = render.find_font(self.style)
        for shot in timeline["shots"]:
            with self.subTest(shot=shot["id"]):
                context = render._shot_context(shot, self.style)
                context["font_path"] = font
                first = render._render_low_frame(shot, 0, self.style, context)
                later = render._render_low_frame(shot, 8, self.style, context)
                self.assertEqual(first.size, (320, 180))
                self.assertNotEqual(first.tobytes(), later.tobytes())
        closing = timeline["shots"][-1]
        context = render._shot_context(closing, self.style)
        context["font_path"] = font
        previous = None
        freeze_run = 0
        max_freeze_run = 0
        for local_frame in range(100):
            current = render._render_low_frame(closing, local_frame, self.style, context).tobytes()
            freeze_run = freeze_run + 1 if current == previous else 0
            max_freeze_run = max(max_freeze_run, freeze_run)
            previous = current
        self.assertLess(max_freeze_run, 90)
        subtitle = timeline["subtitles"][0]
        prepared = render._prepare_subtitle(subtitle, self.style, font, 1280, 720)
        x0, y0, x1, y1 = prepared["box_bounds"]
        self.assertGreater(x0, 32)
        self.assertLess(x1, 1248)
        self.assertGreaterEqual(y0, 32)
        self.assertLessEqual(y1, 688)
        self.assertLess(x1 - x0, 1280)
        self.assertEqual(prepared["font"].size, 42)
        self.assertEqual(prepared["corner_radius"], 0)
        self.assertEqual(prepared["border_width"], 4)
        self.assertEqual(prepared["band_top"], 540)

    def test_reference_split_and_closeup_pixels(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        font = render.find_font(self.style)
        opening = timeline["shots"][1]
        context = render._shot_context(opening, self.style)
        context["font_path"] = font
        split = render._render_low_frame(opening, 20, self.style, context)
        self.assertNotEqual(split.crop((0, 0, 159, 180)).tobytes(), split.crop((161, 0, 320, 180)).tobytes())
        ink = render._rgb("ink", self.style["palette"])
        self.assertEqual(split.getpixel((159, 90)), ink)
        self.assertEqual(split.getpixel((160, 90)), ink)
        for action in ("embrace", "heart_glass"):
            shot = next(item for item in timeline["shots"] if item["action"] == action)
            context = render._shot_context(shot, self.style)
            context["font_path"] = font
            frame = render._render_low_frame(shot, 30, self.style, context)
            self.assertEqual(frame.size, (320, 180))
            self.assertGreater(len(frame.getcolors(maxcolors=320 * 180) or []), 12)

    def test_semantic_scenes_and_epilogue_montage(self) -> None:
        aligned = {
            "duration": 32.0,
            "status": "aligned",
            "title": "日落大道",
            "lines": [
                {"id": 0, "text": "我们寻找着在这条路的中间，公路向远方", "start": 2.0, "end": 5.0, "status": "aligned"},
                {"id": 1, "text": "每当黄昏阳光把所有都渲染", "start": 6.0, "end": 9.0, "status": "aligned"},
            ],
        }
        features = {"rms_times": [0, 8, 16, 24, 32], "rms": [0.2, 0.5, 0.8, 0.6, 0.3]}
        timeline = render.plan_timeline(aligned, features, self.style)
        self.assertEqual(timeline["story_theme"], "highway")
        lyric_shots = [shot for shot in timeline["shots"] if shot["line_id"] is not None]
        self.assertEqual([shot["scene"] for shot in lyric_shots], ["highway", "seaside"])
        epilogue = [shot for shot in timeline["shots"] if shot["story_phase"] == "epilogue"]
        self.assertGreaterEqual(len(epilogue), 3)
        self.assertTrue(all(shot["line_id"] is None for shot in epilogue))
        signatures = {
            (shot["scene"], shot["shot_type"], shot["action"], shot["blocking"]["layout"])
            for shot in epilogue
        }
        self.assertGreaterEqual(len(signatures), 3)
        self.assertEqual(epilogue[-1]["action"], "title_board")
        last_cue_end = timeline["lyric_cues"][-1]["end_frame"]
        self.assertTrue(all(row["visual_line_id"] is None for row in render.build_frame_bindings(timeline)[last_cue_end:]))

    def test_reference_v4_compositions_enter_timeline(self) -> None:
        shots = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)["shots"]
        self.assertIn("train_interior", {shot["scene"] for shot in shots})
        self.assertGreaterEqual(
            {shot["blocking"]["layout"] for shot in shots},
            {"bench_pair", "dual_portrait", "close_pair"},
        )
        self.assertGreaterEqual(
            {shot["action"] for shot in shots},
            {"title_board", "embrace", "heart_glass"},
        )

    def test_v4_output_grid_is_fixed_4x4_blocks(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        self.assertEqual((timeline["width"], timeline["height"]), (1280, 720))
        self.assertEqual((timeline["low_width"], timeline["low_height"]), (320, 180))
        shot = timeline["shots"][1]
        context = render._shot_context(shot, self.style)
        context["font_path"] = render.find_font(self.style)
        low = render._render_low_frame(shot, 20, self.style, context)
        high = low.resize((1280, 720), Image.Resampling.NEAREST)
        blocks = np.asarray(high).reshape(180, 4, 320, 4, 3)
        self.assertTrue((blocks == blocks[:, 0:1, :, 0:1, :]).all())

    def test_iris_transition_reaches_black(self) -> None:
        current = Image.new("RGB", (480, 270), (255, 255, 255))
        black = Image.new("RGB", current.size, (0, 0, 0))
        palette = render._palette_image(self.style)
        start = render._transition(current, black, "iris_out", 0.0, palette)
        end = render._transition(current, black, "iris_out", 1.0, palette)
        self.assertEqual(start.getextrema(), ((255, 255), (255, 255), (255, 255)))
        self.assertEqual(end.getextrema(), ((0, 0), (0, 0), (0, 0)))

    def test_full_reference_direction_actions_and_style_qc(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        actions = {shot["action"] for shot in timeline["shots"]}
        effects = {effect for shot in timeline["shots"] for effect in shot["effects"]}
        self.assertGreaterEqual(actions, {"title_board", "reading", "cycling", "embrace", "heart_glass"})
        self.assertGreaterEqual(effects, {"thought_bubble", "fireworks", "photo_frame"})
        self.assertTrue(all(shot["camera"]["kind"] for shot in timeline["shots"]))
        report = qc.validate_style_profile(timeline, self.style)
        self.assertTrue(report["passed"], report["checks"])
        self.assertTrue(report["art_review_required"])
        font = render.find_font(self.style)
        for action in ("reading", "cycling"):
            shot = next(item for item in timeline["shots"] if item["action"] == action)
            context = render._shot_context(shot, self.style)
            context["font_path"] = font
            first = render._render_low_frame(shot, 0, self.style, context)
            later = render._render_low_frame(shot, 12, self.style, context)
            self.assertEqual(first.size, (320, 180))
            self.assertNotEqual(first.tobytes(), later.tobytes())

    def test_reference_transition_renderers(self) -> None:
        previous = Image.new("RGB", (480, 270), (32, 64, 96))
        ImageDraw.Draw(previous).rectangle((190, 80, 290, 190), fill=(200, 80, 70))
        current = Image.new("RGB", (480, 270), (240, 220, 190))
        palette = render._palette_image(self.style)
        for name in ("memory_dissolve", "film_wipe", "photo_reveal", "match_cut"):
            with self.subTest(name=name):
                middle = render._transition(current, previous, name, 0.5, palette)
                self.assertEqual(middle.size, current.size)
                self.assertNotEqual(middle.tobytes(), current.tobytes())
                self.assertNotEqual(middle.tobytes(), previous.tobytes())
                if name != "film_wipe":
                    end = render._transition(current, previous, name, 1.0, palette)
                    self.assertEqual(end.tobytes(), current.tobytes())

    def test_preview_includes_motion_transitions_and_style_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = preview.generate_preview(list(preview._SAMPLE_LYRICS), self.style, temporary)
            root = Path(temporary)
            self.assertTrue((root / "motion-contact-sheet.jpg").is_file())
            self.assertTrue((root / "transition-contact-sheet.jpg").is_file())
            self.assertTrue((root / "weather-contact-sheet.jpg").is_file())
            self.assertTrue((root / "weather-transition-contact-sheet.jpg").is_file())
            self.assertTrue((root / "style-qc.json").is_file())
            self.assertTrue(result["manifest"]["motion_sequences"])
            self.assertTrue(result["manifest"]["transition_sequences"])
            self.assertEqual(
                {record["weather"] for record in result["manifest"]["weather_sequences"]},
                {"day", "cloudy", "rain", "snow", "sunset", "night"},
            )
            self.assertEqual(len(result["manifest"]["weather_transition_sequences"]), 5)
            motion_actions = {record["action"] for record in result["manifest"]["motion_sequences"]}
            transition_types = {record["type"] for record in result["manifest"]["transition_sequences"]}
            self.assertGreaterEqual(motion_actions, {"cycling", "heart_glass"})
            self.assertIn("iris_out", transition_types)
            self.assertTrue(result["manifest"]["style_qc"]["passed"])

    def test_sync_boundary_and_invalid_alignment(self) -> None:
        self.assertFalse(qc._exceeds(50.0, 50.0))
        self.assertTrue(qc._exceeds(50.001, 50.0))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "alignment.json"
            value = _alignment(4.0)
            value["lines"] = value["lines"][:2]
            value["lines"][1].update(start=0.2, end=3.0)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重叠或非单调"):
                mv._provided_alignment(path, ["第一句", "第二句"], 4.0)

    def test_provided_alignment_rejects_invalid_status_and_confidence(self) -> None:
        cases = [
            ("status", "estimate", "status"),
            ("confidence", float("nan"), "置信度"),
            ("confidence", float("inf"), "置信度"),
            ("confidence", -0.01, "置信度"),
            ("confidence", 1.01, "置信度"),
            ("confidence", None, "置信度"),
            ("confidence", "bad", "置信度"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "alignment.json"
            for field, invalid, message in cases:
                with self.subTest(field=field, invalid=invalid):
                    value = _alignment(10.0)
                    value["lines"] = value["lines"][:2]
                    value["lines"][0][field] = invalid
                    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        mv._provided_alignment(path, ["第一句", "第二句"], 10.0)

    def test_estimated_line_forces_draft_and_defaults_confidence(self) -> None:
        value = _alignment(10.0)
        value["lines"] = value["lines"][:2]
        value["lines"][0].pop("confidence")
        value["lines"][1].update(status="estimated")
        value["lines"][1].pop("confidence")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "alignment.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            result = mv._provided_alignment(path, ["第一句", "第二句"], 10.0)
        self.assertEqual(result["status"], "estimated")
        self.assertEqual([line["confidence"] for line in result["lines"]], [1.0, 0.0])

    def test_shared_lyric_cue_binding_and_boundary_faults(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        report = qc.validate_timeline(timeline, self.style)
        self.assertTrue(report["passed"], report["errors"])
        self.assertTrue(report["lyric_binding_pass"])
        self.assertEqual(report["metrics"]["lyric_frame_match_ratio"], 1.0)
        bindings = render.build_frame_bindings(timeline)
        self.assertEqual(len(bindings), timeline["frame_count"])
        cues_by_line = {cue["line_id"]: cue for cue in timeline["lyric_cues"]}
        active = 0
        for binding in bindings:
            if binding["lyric_cue_id"] is None:
                continue
            active += 1
            self.assertEqual(binding["visual_line_id"], binding["lyric_line_id"])
            cue = cues_by_line[binding["lyric_line_id"]]
            self.assertEqual(binding["lyric_cue_id"], cue["id"])
            self.assertEqual(binding["subtitle_id"], cue["subtitle_id"])
        self.assertEqual(active, report["metrics"]["active_lyric_frame_count"])

        moved_shot = copy.deepcopy(timeline)
        cue = moved_shot["lyric_cues"][1]
        boundary = next(
            index for index, shot in enumerate(moved_shot["shots"])
            if index and shot["start_frame"] == cue["start_frame"]
        )
        moved_shot["shots"][boundary - 1]["end_frame"] += 1
        moved_shot["shots"][boundary]["start_frame"] += 1
        shot_report = qc.validate_timeline(moved_shot, self.style)
        self.assertFalse(shot_report["lyric_binding_pass"])
        self.assertLess(shot_report["metrics"]["lyric_frame_match_ratio"], 1.0)

        moved_subtitle = copy.deepcopy(timeline)
        moved_subtitle["subtitles"][0]["start_frame"] += 1
        subtitle_report = qc.validate_timeline(moved_subtitle, self.style)
        self.assertFalse(subtitle_report["lyric_binding_pass"])
        self.assertFalse(subtitle_report["subtitle_pass"])

    def test_lyric_quantization_rejects_indistinguishable_lines(self) -> None:
        lines = [
            {"id": 0, "text": "第一句", "start": 0.01, "end": 0.20},
            {"id": 1, "text": "第二句", "start": 0.02, "end": 0.40},
        ]
        with self.assertRaisesRegex(ValueError, "同一视频帧"):
            render._build_lyric_cues(lines, 30, 120)
        with self.assertRaisesRegex(ValueError, "短于一个视频帧"):
            render._build_lyric_cues([{"id": 0, "text": "短句", "start": 0.01, "end": 0.02}], 30, 120)

    def test_frames_log_binding_and_weather_fault_injection(self) -> None:
        aligned = _alignment(4.0)
        aligned["lines"] = aligned["lines"][:2]
        aligned["lines"][0].update(text="教室里的信", start=0.0, end=1.8)
        aligned["lines"][1].update(text="雨夜红伞", start=1.8, end=3.8)
        timeline = render.plan_timeline(aligned, {"beats": [], "onsets": []}, self.style)
        bindings = render.build_frame_bindings(timeline)
        transitions = qc._transition_schedule(timeline, self.style, timeline["frame_count"])
        transition_map = qc._transition_frame_map(transitions, timeline["frame_count"])
        rows = []
        for binding in bindings:
            row = dict(binding)
            row.update(
                scene="campus",
                transition=transition_map[binding["frame"]] or ("hard_cut" if binding["local_frame"] == 0 else None),
                mean_luma=100.0,
                palette_size=20,
                frame_difference=0.0 if binding["frame"] == 0 else 1.0,
                subtitle_active=binding["subtitle_id"] is not None,
                subtitle_overflow=False,
                contrast=10.0,
                source_black=False,
            )
            rows.append(row)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frames.jsonl"

            def write(values: list[dict]) -> None:
                path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in values), encoding="utf-8")

            write(rows)
            baseline = qc.validate_frames_log(path, timeline, self.style)
            self.assertTrue(baseline["passed"], baseline["errors"])
            self.assertEqual(baseline["metrics"]["lyric_frame_match_ratio"], 1.0)
            self.assertEqual(baseline["metrics"]["weather_frame_match_ratio"], 1.0)

            broken_lyric = copy.deepcopy(rows)
            active = next(row for row in broken_lyric if row["lyric_cue_id"] is not None)
            active["visual_line_id"] = -1
            write(broken_lyric)
            lyric_report = qc.validate_frames_log(path, timeline, self.style)
            self.assertFalse(lyric_report["lyric_binding_pass"])
            self.assertLess(lyric_report["metrics"]["lyric_frame_match_ratio"], 1.0)

            broken_weather = copy.deepcopy(rows)
            changing = next(row for row in broken_weather if row["weather_transition"])
            changing["weather_transition_stage"] += 1
            write(broken_weather)
            weather_report = qc.validate_frames_log(path, timeline, self.style)
            self.assertFalse(weather_report["weather_pass"])
            self.assertLess(weather_report["metrics"]["weather_transition_match_ratio"], 1.0)

    def test_weather_vocabulary_and_ordered_transition(self) -> None:
        expected = {
            "晴天阳光": "day",
            "多云阴云": "cloudy",
            "下雨": "rain",
            "初雪": "snow",
            "晚霞": "sunset",
            "黄昏阳光": "sunset",
            "星空": "night",
        }
        for text, weather in expected.items():
            with self.subTest(weather=weather):
                self.assertEqual(render._shot_weather("campus", "campus", "development", text, 0.2), weather)

        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        shot = next(shot for shot in timeline["shots"] if shot.get("weather_transition_frames", 0) >= 6)
        length = shot["weather_transition_frames"]
        states = [render.art.weather_state(shot, frame) for frame in (0, length // 2, length - 1, length)]
        self.assertTrue(all(state["active"] for state in states[:3]))
        self.assertFalse(states[-1]["active"])
        self.assertEqual([state["amount"] for state in states], sorted(state["amount"] for state in states))
        self.assertEqual(states[-1]["amount"], 1.0)
        context = render._shot_context(shot, self.style)
        context["font_path"] = render.find_font(self.style)
        images = [render._render_low_frame(shot, frame, self.style, context) for frame in (0, length // 2, length - 1, length)]
        self.assertGreater(len({image.tobytes() for image in images}), 2)
        stable = dict(shot)
        stable.update(weather_from=None, weather_transition_frames=0)
        stable_context = render._shot_context(stable, self.style)
        stable_context["font_path"] = context["font_path"]
        self.assertEqual(
            images[-1].tobytes(),
            render._render_low_frame(stable, length, self.style, stable_context).tobytes(),
        )

    def test_all_weather_states_have_distinct_dynamic_pixels(self) -> None:
        timeline = preview.preview_timeline(list(preview._SAMPLE_LYRICS), self.style)
        base = dict(next(shot for shot in timeline["shots"] if shot.get("scene") == "station"))
        base.update(
            scene="station",
            shot_type="wide",
            action="waiting",
            characters=["protagonist"],
            compare_weather=None,
            story_phase="development",
            effects=[],
            weather_from=None,
            weather_transition_frames=0,
        )
        first_frames = set()
        for weather in ("day", "cloudy", "rain", "snow", "sunset", "night"):
            with self.subTest(weather=weather):
                shot = dict(base)
                shot.update(id=f"test_{weather}", weather=weather)
                context = render._shot_context(shot, self.style)
                context["font_path"] = render.find_font(self.style)
                first = render._render_low_frame(shot, 0, self.style, context)
                later = render._render_low_frame(shot, 8, self.style, context)
                self.assertNotEqual(first.tobytes(), later.tobytes())
                first_frames.add(first.tobytes())
        self.assertEqual(len(first_frames), 6)

    def test_subtitle_layout_and_font_failure(self) -> None:
        font = render.find_font(self.style)
        prepared = render._prepare_subtitle(
            {"text": "这是一句用于检查自动换行和安全区的较长虚构歌词"},
            self.style,
            font,
            1280,
            720,
        )
        self.assertFalse(prepared["overflow"])
        self.assertLessEqual(len(prepared["lines"]), 2)
        with self.assertRaisesRegex(ValueError, "过长|安全区"):
            render._prepare_subtitle({"text": "超长歌词" * 100}, self.style, font, 1280, 720)
        with mock.patch("render.Path.is_file", return_value=True), mock.patch(
            "render.ImageFont.truetype", side_effect=OSError("no font")
        ):
            with self.assertRaisesRegex(RuntimeError, "找不到可用的中文"):
                render.find_font(self.style)

    def test_corrupt_audio_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.wav"
            path.write_bytes(b"not an audio file")
            with self.assertRaises(RuntimeError):
                media.probe_media(path)

    def test_contact_sheet_uses_video_stream_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            video.write_bytes(b"test placeholder")
            image_buffer = io.BytesIO()
            Image.new("RGB", (960, 270), (20, 30, 40)).save(image_buffer, format="PNG")
            completed = mock.Mock(returncode=0, stdout=image_buffer.getvalue(), stderr=b"")
            probe = {
                "format": {"duration": "10.1"},
                "streams": [
                    {"codec_type": "video", "duration": "10.0"},
                    {"codec_type": "audio", "duration": "10.1"},
                ],
            }
            with mock.patch.object(qc, "_probe_media", return_value=probe), mock.patch.object(
                qc, "_run_external", return_value=completed
            ) as external:
                qc.generate_contact_sheet(video, root / "contact-sheet.jpg", 5.0)
            command = external.call_args.args[0]
            filter_graph = command[command.index("-vf") + 1]
            self.assertIn("tile=2x1", filter_graph)
            with Image.open(root / "contact-sheet.jpg") as sheet:
                self.assertEqual(sheet.size, (960, 270))


@unittest.skipUnless(os.environ.get("PIXEL_MV_E2E") == "1", "set PIXEL_MV_E2E=1 for real rendering")
class EndToEndTests(unittest.TestCase):
    def test_aligned_generation_and_full_qc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "song.wav"
            _write_pcm(audio, _song(10.0), 22050)
            reference = root / "reference.json"
            reference.write_text(json.dumps(_alignment(10.0), ensure_ascii=False), encoding="utf-8")
            result = mv.generate(
                "教室里的那封信\n雨夜里等你回来\n海边日落和照片\n我们还唱着歌",
                audio,
                root / "out",
                alignment_path=reference,
            )
            self.assertTrue(result["passed"], (root / "out" / "qc.json").read_text(encoding="utf-8"))
            self.assertFalse(result["draft"])
            self.assertEqual(result["frame_count"], 300)
            self.assertEqual(result["human_vocal_sync"], "unverified")
            self.assertLessEqual(abs(result["audio_offset_ms"]), 50.0)
            report = json.loads((root / "out" / "qc.json").read_text(encoding="utf-8"))
            self.assertTrue(report["lyric_binding_pass"], report["errors"])
            self.assertTrue(report["weather_pass"], report["errors"])
            self.assertEqual(report["metrics"]["timeline"]["lyric_frame_match_ratio"], 1.0)
            self.assertEqual(report["metrics"]["frames_log"]["lyric_frame_match_ratio"], 1.0)
            self.assertEqual(report["metrics"]["frames_log"]["weather_frame_match_ratio"], 1.0)
            self.assertTrue((root / "out" / "final.mp4").is_file())
            self.assertTrue((root / "out" / "contact-sheet.jpg").is_file())
            rows = [json.loads(line) for line in (root / "out" / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 300)
            self.assertEqual([row["frame"] for row in rows[-18:] if row["transition"] == "iris_out"], list(range(282, 300)))
            self.assertTrue(rows[-1]["source_black"])

    def test_two_inputs_can_return_complete_estimated_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "short.wav"
            _write_pcm(audio, _song(2.0), 22050)
            with mock.patch("media.align_lyrics", return_value=_alignment(2.0, "estimated")):
                result = mv.generate("第一句虚构歌词\n第二句虚构歌词", audio, root / "draft-out")
            report = json.loads((root / "draft-out" / "qc.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"], report["errors"])
            self.assertTrue(report["draft"])
            self.assertTrue(report["lyric_binding_pass"], report["errors"])
            self.assertTrue(report["weather_pass"], report["errors"])
            self.assertEqual(report["metrics"]["frames_log"]["lyric_frame_match_ratio"], 1.0)
            self.assertEqual(report["human_vocal_sync"], "unverified")
            self.assertTrue(result["draft"])
            self.assertTrue((root / "draft-out" / "draft.mp4").is_file())

            style = render.load_style()
            timeline = json.loads((root / "draft-out" / "timeline.json").read_text(encoding="utf-8"))
            delayed = root / "draft-out" / "work" / "injected-delay.mp4"
            mv._mux_audio(
                root / "draft-out" / "work" / "video-only.mp4",
                audio,
                delayed,
                style,
                shift_seconds=0.075,
            )
            measurement = media.measure_audio_offset(audio, delayed)
            self.assertGreater(measurement["offset_ms"], 50.0)
            delayed_qc = qc.validate_output(
                delayed,
                audio,
                timeline,
                style,
                root / "draft-out" / "work",
            )
            self.assertFalse(delayed_qc["audio_sync_pass"])

            corrected = root / "draft-out" / "work" / "injected-delay-corrected.mp4"
            mv._mux_audio(
                root / "draft-out" / "work" / "video-only.mp4",
                delayed,
                corrected,
                style,
                shift_seconds=-measurement["offset_ms"] / 1000.0,
            )
            corrected_measurement = media.measure_audio_offset(audio, corrected)
            self.assertLessEqual(abs(corrected_measurement["offset_ms"]), 50.0)
            corrected_qc = qc.validate_output(
                corrected,
                audio,
                timeline,
                style,
                root / "draft-out" / "work",
            )
            self.assertTrue(corrected_qc["audio_sync_pass"], corrected_qc["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
