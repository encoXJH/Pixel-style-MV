from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import media
import qc
import render


_SAMPLE_LYRICS = (
    "海风穿过安静的车站",
    "你提着行李看向远方",
    "我们沿着海岸走过黄昏",
    "站台前我们靠近相拥",
    "房间的窗下安静阅读",
    "骑着单车穿过长长的公路",
    "雨里我收好那封信",
    "在雾气玻璃上画下一颗心",
    "梦里还留着那张照片",
    "海边的烟花照亮夜空",
    "我们拿起吉他唱着歌",
    "再回到海边等你出现",
)


def _lyrics(value: str | None) -> list[str]:
    if value is None:
        return list(_SAMPLE_LYRICS)
    path = Path(value).expanduser()
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else value
    return media.parse_lyrics(text)


def preview_timeline(lines: list[str], style: dict) -> dict:
    """Build an explicitly estimated timeline for art review, without audio."""
    lead = 0.8
    line_step = 3.2
    duration = lead + len(lines) * line_step + 1.6
    aligned_lines = []
    for index, text in enumerate(lines):
        start = lead + index * line_step
        aligned_lines.append(
            {
                "id": index,
                "text": text,
                "start": start,
                "end": start + 2.7,
                "status": "estimated",
                "confidence": 0.0,
            }
        )
    alignment = {
        "duration": duration,
        "status": "estimated",
        "confidence": 0.0,
        "human_vocal_sync": "unverified",
        "lines": aligned_lines,
    }
    features = {
        "duration": duration,
        "beats": [index * 0.5 for index in range(int(duration * 2))],
        "onsets": [],
        "rms_times": [index * 0.5 for index in range(int(duration * 2) + 1)],
        "rms": [0.45 + 0.25 * math.sin(index * 0.7) for index in range(int(duration * 2) + 1)],
    }
    return render.plan_timeline(alignment, features, style)


def _selected_shots(shots: list[dict]) -> list[dict]:
    if len(shots) <= 12:
        return list(shots)
    selected: list[dict] = []
    for shot in shots:
        if shot.get("story_phase") in ("prologue", "epilogue") or shot.get("action") in ("embrace", "heart_glass", "cycling", "reading") or set(shot.get("effects", [])) & {"thought_bubble", "fireworks", "photo_frame"}:
            if shot not in selected:
                selected.append(shot)
    for shot in shots:
        if len(selected) >= 12:
            break
        if shot not in selected:
            selected.append(shot)
    selected.sort(key=lambda shot: int(shot["start_frame"]))
    if len(selected) > 12:
        indices = sorted({round(index * (len(selected) - 1) / 11) for index in range(12)})
        selected = [selected[index] for index in indices]
    return selected


def _sequence_sheet(groups: list[tuple[str, list[Image.Image]]], path: Path, font_path: str) -> None:
    if not groups:
        return
    columns = max(len(images) for _, images in groups)
    label_height = 28
    sheet = Image.new("RGB", (columns * 240, len(groups) * (135 + label_height)), (244, 235, 221))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(font_path, 16)
    for row, (label, images) in enumerate(groups):
        y = row * (135 + label_height)
        for column, image in enumerate(images):
            sheet.paste(image.resize((240, 135), Image.Resampling.NEAREST), (column * 240, y))
        draw.text((8, y + 139), label, font=font, fill=(37, 35, 42))
    sheet.save(path, quality=92)


def _motion_previews(timeline: dict, style: dict, output: Path, font_path: str) -> tuple[list[dict], list[tuple[str, list[Image.Image]]]]:
    shots = [
        shot for shot in timeline["shots"]
        if shot.get("action") in ("train", "walking", "cycling", "embrace", "heart_glass", "playing")
        or shot.get("camera", {}).get("kind") != "static"
        or set(shot.get("effects", [])) & {"fireworks", "pixel_hearts"}
    ]
    records = []
    groups = []
    for shot in shots:
        directory = output / "motion" / shot["id"]
        directory.mkdir(parents=True, exist_ok=True)
        context = render._shot_context(shot, style)
        context["font_path"] = font_path
        length = int(shot["end_frame"]) - int(shot["start_frame"])
        last = min(max(0, length - 1), 90)
        local_frames = sorted({round(index * last / 5) for index in range(6)})
        files = []
        images = []
        for sequence_index, local_frame in enumerate(local_frames):
            image = render._render_low_frame(shot, local_frame, style, context)
            filename = f"{sequence_index:02d}_f{local_frame:04d}.png"
            image.save(directory / filename)
            files.append(f"motion/{shot['id']}/{filename}")
            images.append(image)
        label = f"{shot['id']}  {shot['action']} / {shot['camera'].get('kind', 'static')} / {','.join(shot.get('effects', [])) or 'no_overlay'}"
        groups.append((label, images))
        records.append({"shot_id": shot["id"], "action": shot["action"], "camera": shot["camera"], "local_frames": local_frames, "files": files})
    return records, groups


def _transition_previews(timeline: dict, style: dict, output: Path, font_path: str) -> tuple[list[dict], list[tuple[str, list[Image.Image]]]]:
    shots = timeline["shots"]
    records = []
    groups = []
    palette = render._palette_image(style)
    for index in range(1, len(shots)):
        shot = shots[index]
        name = str(shot.get("transition_in", "hard_cut"))
        frames = int(shot.get("transition_frames", 0))
        if name == "hard_cut" or frames <= 0:
            continue
        previous = shots[index - 1]
        previous_context = render._shot_context(previous, style)
        current_context = render._shot_context(shot, style)
        previous_context["font_path"] = font_path
        current_context["font_path"] = font_path
        previous_length = int(previous["end_frame"]) - int(previous["start_frame"])
        old = render._render_low_frame(previous, max(0, previous_length - 1), style, previous_context)
        directory = output / "transitions" / shot["id"]
        directory.mkdir(parents=True, exist_ok=True)
        local_frames = sorted({min(frames - 1, round(step * (frames - 1) / 3)) for step in range(4)})
        files = []
        images = []
        for sequence_index, local_frame in enumerate(local_frames):
            current = render._render_low_frame(shot, local_frame, style, current_context)
            image = render._transition(current, old, name, (local_frame + 1) / frames, palette)
            filename = f"{sequence_index:02d}_f{local_frame:04d}.png"
            image.save(directory / filename)
            files.append(f"transitions/{shot['id']}/{filename}")
            images.append(image)
        groups.append((f"{shot['id']}  {name} / {frames} frames", images))
        records.append({"shot_id": shot["id"], "type": name, "duration_frames": frames, "files": files})
    if shots:
        shot = shots[-1]
        name = str(shot.get("transition_out", "hard_cut"))
        frames = int(shot.get("transition_out_frames", 0))
        if name != "hard_cut" and frames > 0:
            context = render._shot_context(shot, style)
            context["font_path"] = font_path
            length = int(shot["end_frame"]) - int(shot["start_frame"])
            transition_start = max(0, length - frames)
            directory = output / "transitions" / f"{shot['id']}_out"
            directory.mkdir(parents=True, exist_ok=True)
            local_frames = sorted({min(frames - 1, round(step * (frames - 1) / 3)) for step in range(4)})
            files = []
            images = []
            for sequence_index, local_frame in enumerate(local_frames):
                current = render._render_low_frame(shot, transition_start + local_frame, style, context)
                image = render._transition(current, Image.new("RGB", current.size, (0, 0, 0)), name, (local_frame + 1) / frames, palette)
                filename = f"{sequence_index:02d}_f{local_frame:04d}.png"
                image.save(directory / filename)
                files.append(f"transitions/{shot['id']}_out/{filename}")
                images.append(image)
            groups.append((f"{shot['id']}_out  {name} / {frames} frames", images))
            records.append({"shot_id": shot["id"], "type": name, "duration_frames": frames, "direction": "out", "files": files})
    return records, groups


def generate_preview(lines: list[str], style: dict, output_dir: str | Path) -> dict:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    timeline = preview_timeline(lines, style)
    font_path = render.find_font(style)
    width = int(timeline["width"])
    height = int(timeline["height"])
    prepared = {
        subtitle["id"]: render._prepare_subtitle(subtitle, style, font_path, width, height)
        for subtitle in timeline["subtitles"]
    }
    selected = _selected_shots(timeline["shots"])
    records = []
    frames = []
    for index, shot in enumerate(selected, 1):
        local_frame = max(0, (shot["end_frame"] - shot["start_frame"] - 1) // 2)
        absolute_frame = shot["start_frame"] + local_frame
        context = render._shot_context(shot, style)
        context["font_path"] = font_path
        low = render._render_low_frame(shot, local_frame, style, context)
        frame = low.resize((width, height), Image.Resampling.NEAREST)
        active = [
            subtitle
            for subtitle in timeline["subtitles"]
            if subtitle["start_frame"] <= absolute_frame < subtitle["end_frame"]
        ]
        for subtitle in active:
            render._draw_subtitle(frame, prepared[subtitle["id"]])
        filename = f"{index:02d}_{shot['scene']}_{shot['shot_type']}_{shot['weather']}.png"
        frame.save(output / filename)
        frames.append((frame, shot))
        records.append(
            {
                "file": filename,
                "shot_id": shot["id"],
                "scene": shot["scene"],
                "shot_type": shot["shot_type"],
                "weather": shot["weather"],
                "story_phase": shot["story_phase"],
                "action": shot["action"],
                "camera": shot.get("camera"),
                "effects": shot.get("effects", []),
                "transition_in": shot.get("transition_in"),
                "subtitle": active[-1]["text"] if active else None,
            }
        )

    columns = 3
    label_height = 30
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * 480, rows * (270 + label_height)), (244, 235, 221))
    draw = ImageDraw.Draw(sheet)
    label_font = ImageFont.truetype(font_path, 18)
    for index, (frame, shot) in enumerate(frames):
        column = index % columns
        row = index // columns
        x = column * 480
        y = row * (270 + label_height)
        sheet.paste(frame.resize((480, 270), Image.Resampling.NEAREST), (x, y))
        label = f"{index + 1:02d}  {shot['scene']} / {shot['shot_type']} / {shot['weather']}"
        draw.text((x + 10, y + 274), label, font=label_font, fill=(37, 35, 42))
    sheet.save(output / "contact-sheet.jpg", quality=92)

    motion_records, motion_groups = _motion_previews(timeline, style, output, font_path)
    _sequence_sheet(motion_groups, output / "motion-contact-sheet.jpg", font_path)
    transition_records, transition_groups = _transition_previews(timeline, style, output, font_path)
    _sequence_sheet(transition_groups, output / "transition-contact-sheet.jpg", font_path)
    style_report = qc.validate_style_profile(timeline, style)
    (output / "style-qc.json").write_text(json.dumps(style_report, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "schema_version": 2,
        "preview_only": True,
        "purpose": "art_and_composition_review",
        "timing_status": "estimated_equal_duration",
        "audio_provided": False,
        "audio_sync": "not_evaluated",
        "human_vocal_sync": "unverified",
        "art_version": timeline["art_version"],
        "story_theme": timeline["story_theme"],
        "style_profile": timeline.get("style_profile"),
        "frames": records,
        "motion_sequences": motion_records,
        "transition_sequences": transition_records,
        "style_qc": style_report,
        "review_required": ["character_consistency", "action_readability", "transition_continuity", "subtitle_clearance", "density_balance"],
    }
    (output / "preview.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output_dir": str(output), "frame_count": len(records), "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description="仅生成像素美术与字幕预览，不生成或改写歌曲 MV")
    parser.add_argument("--lyrics", help="歌词文件路径或直接文本；省略时使用内置虚构样例")
    parser.add_argument("--output-dir", required=True, help="独立预览输出目录")
    parser.add_argument("--style", help="可选 style.json 路径")
    args = parser.parse_args()
    style = render.load_style(args.style)
    lines = _lyrics(args.lyrics)
    result = generate_preview(lines, style, args.output_dir)
    print(json.dumps({"output_dir": result["output_dir"], "frame_count": result["frame_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
