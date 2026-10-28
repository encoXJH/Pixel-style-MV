from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import sound


def _timeline(shots: list[dict], duration: float | None = None, fps: int = 10) -> dict:
    frame_count = int(round((duration if duration is not None else len(shots)) * fps))
    return {
        "duration": frame_count / fps,
        "frame_count": frame_count,
        "fps": fps,
        "seed": 20261027,
        "shots": shots,
    }


def _shot(index: int, frame_count: int = 10, **values) -> dict:
    shot = {
        "id": f"shot_{index:04d}",
        "start_frame": index * frame_count,
        "end_frame": (index + 1) * frame_count,
        "scene": "generic",
        "weather": "day",
        "action": "waiting",
        "effects": [],
        "sfx": [],
        "transition": "hard_cut",
        "seed": 9000 + index,
    }
    shot.update(values)
    return shot


def _read_wav(path: Path) -> tuple[wave._wave_params, np.ndarray]:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    return params, samples.reshape(-1, params.nchannels)


class SoundDesignTests(unittest.TestCase):
    def test_deterministic_default_format(self) -> None:
        timeline = _timeline(
            [_shot(0, weather="rain", sfx=["bicycle_bell"])], duration=1.2
        )
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root, second_root = Path(first), Path(second)
            first_manifest = sound.create_sound_design(timeline, {}, first_root)
            second_manifest = sound.create_sound_design(timeline, {}, second_root)
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(
                (first_root / "sound-cues.wav").read_bytes(),
                (second_root / "sound-cues.wav").read_bytes(),
            )
            self.assertEqual(
                (first_root / "sound-design.json").read_bytes(),
                (second_root / "sound-design.json").read_bytes(),
            )
            params, samples = _read_wav(first_root / "sound-cues.wav")
            self.assertEqual(params.framerate, 48000)
            self.assertEqual(params.nchannels, 2)
            self.assertEqual(params.sampwidth, 2)
            self.assertGreater(np.count_nonzero(samples), 0)

    def test_wav_manifest_and_supported_cues(self) -> None:
        shots = [
            _shot(0, weather="rain"),
            _shot(1, weather="snow"),
            _shot(2, scene="ocean"),
            _shot(3, action="train"),
            _shot(4, action="walking", sfx="bicycle_bell"),
            _shot(5, effects=["page_turn", "camera_shutter"]),
            _shot(6, sfx={"type": "fireworks"}),
            _shot(7, scene="stage", transition="white_flash"),
        ]
        timeline = _timeline(shots)
        style = {
            "sound": {
                "sample_rate": 12000,
                "cue_gain": 0.2,
                "max_cue_gain": 0.15,
                "fade_seconds": 0.02,
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = sound.create_sound_design(timeline, style, root)
            saved = json.loads((root / "sound-design.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest, saved)
            self.assertEqual(manifest["schema_version"], 1)
            self.assertIs(manifest["enabled"], True)
            self.assertIs(manifest["original_reference_audio_available"], False)
            self.assertEqual(manifest["music_source"], "user_song")
            self.assertEqual(manifest["design_status"], "original_programmatic_cues")
            self.assertEqual(manifest["sample_rate"], 12000)
            self.assertEqual(manifest["channels"], 2)
            self.assertEqual(manifest["file"], "sound-cues.wav")
            self.assertEqual(manifest["duration"], 8.0)
            self.assertEqual(manifest["cue_count"], len(manifest["cues"]))
            self.assertGreaterEqual(manifest["cue_count"], 11)
            self.assertTrue(math.isfinite(manifest["peak"]))
            self.assertLessEqual(manifest["peak"], 0.75)

            expected_types = {
                "rain",
                "wind",
                "waves",
                "train",
                "footsteps",
                "bicycle_bell",
                "page_turn",
                "camera_shutter",
                "fireworks",
                "room_tone",
                "transition",
            }
            self.assertTrue(expected_types.issubset({cue["type"] for cue in manifest["cues"]}))
            self.assertEqual(
                ["train"],
                [cue["type"] for cue in manifest["cues"] if cue["shot_id"] == "shot_0003"],
            )
            for cue in manifest["cues"]:
                self.assertEqual(
                    {"id", "shot_id", "type", "start", "end", "gain"},
                    set(cue),
                )
                self.assertGreaterEqual(cue["start"], 0.0)
                self.assertLess(cue["start"], cue["end"])
                self.assertLessEqual(cue["end"], manifest["duration"])
                self.assertGreater(cue["gain"], 0.0)
                self.assertLessEqual(cue["gain"], 0.15)
                self.assertLessEqual(cue["end"] - cue["start"], 6.0)

            params, samples = _read_wav(root / "sound-cues.wav")
            self.assertEqual(params.framerate, 12000)
            self.assertEqual(params.nchannels, 2)
            self.assertEqual(params.sampwidth, 2)
            self.assertEqual(params.comptype, "NONE")
            self.assertEqual(params.nframes, int(manifest["duration"] * 12000))
            self.assertEqual(samples.shape, (params.nframes, 2))
            self.assertGreater(np.count_nonzero(samples), 0)
            pcm_peak = float(np.max(np.abs(samples.astype(np.int32)))) / 32767.0
            self.assertAlmostEqual(manifest["peak"], pcm_peak, places=7)

    def test_disabled_writes_silent_valid_outputs(self) -> None:
        timeline = _timeline([_shot(0, weather="rain", sfx="fireworks")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = sound.create_sound_design(
                timeline, {"sound": {"enabled": False}}, root
            )
            params, samples = _read_wav(root / "sound-cues.wav")
            self.assertIs(manifest["enabled"], False)
            self.assertEqual(manifest["cue_count"], 0)
            self.assertEqual(manifest["cues"], [])
            self.assertEqual(manifest["peak"], 0.0)
            self.assertEqual(params.framerate, 48000)
            self.assertEqual(params.nchannels, 2)
            self.assertEqual(params.sampwidth, 2)
            self.assertEqual(params.nframes, 48000)
            self.assertEqual(np.count_nonzero(samples), 0)

    def test_empty_shots_writes_decodable_silence(self) -> None:
        timeline = _timeline([], duration=1.5)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = sound.create_sound_design(timeline, {}, root)
            params, samples = _read_wav(root / manifest["file"])
            self.assertEqual(manifest["duration"], 1.5)
            self.assertEqual(manifest["cue_count"], 0)
            self.assertEqual(manifest["peak"], 0.0)
            self.assertEqual(params.nframes, 72000)
            self.assertEqual(samples.shape, (72000, 2))
            self.assertEqual(np.count_nonzero(samples), 0)

    def test_final_peak_normalization_and_nonfinite_rejection(self) -> None:
        shots = [
            _shot(0, start_frame=0, end_frame=20, id=f"shot_{index:04d}", seed=index, sfx="fireworks")
            for index in range(24)
        ]
        timeline = _timeline(shots, duration=2.0)
        with tempfile.TemporaryDirectory() as temporary:
            manifest = sound.create_sound_design(
                timeline,
                {"sound": {"sample_rate": 8000, "cue_gain": 1.0, "max_cue_gain": 1.0}},
                temporary,
            )
            self.assertGreater(manifest["peak"], 0.0)
            self.assertLessEqual(manifest["peak"], 0.75)
            self.assertTrue(math.isfinite(manifest["peak"]))
            self.assertTrue(all(math.isfinite(cue["gain"]) for cue in manifest["cues"]))

        invalid = [
            ({"duration": math.nan}, {}),
            ({"fps": math.inf}, {}),
            ({}, {"sound": {"cue_gain": math.nan}}),
            ({}, {"sound": {"fade_seconds": math.inf}}),
        ]
        for timeline_patch, style in invalid:
            with self.subTest(timeline_patch=timeline_patch, style=style):
                bad_timeline = _timeline([], duration=1.0)
                bad_timeline.update(timeline_patch)
                with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
                    sound.create_sound_design(bad_timeline, style, temporary)


if __name__ == "__main__":
    unittest.main()
