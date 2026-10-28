from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _palette(style: dict) -> dict[str, tuple[int, int, int]]:
    result = {}
    for name, value in style.get("palette", {}).items():
        value = value.lstrip("#")
        result[name] = tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))
    return result


def _rect(draw: ImageDraw.ImageDraw, box, fill, outline=None, width: int = 1) -> None:
    draw.rectangle(tuple(int(v) for v in box), fill=fill, outline=outline, width=max(1, int(width)))


def _line(draw: ImageDraw.ImageDraw, points, fill, width: int = 1) -> None:
    draw.line(tuple(int(v) for point in points for v in point), fill=fill, width=max(1, int(width)))


def _cloud(draw, x: int, y: int, scale: int, light, shade) -> None:
    s = max(1, scale)
    _rect(draw, (x + 8 * s, y, x + 30 * s, y + 7 * s), light)
    _rect(draw, (x + 2 * s, y + 5 * s, x + 43 * s, y + 13 * s), light)
    _rect(draw, (x + 15 * s, y - 4 * s, x + 26 * s, y + 5 * s), light)
    _rect(draw, (x + 8 * s, y + 13 * s, x + 37 * s, y + 15 * s), shade)
    _rect(draw, (x + 38 * s, y + 8 * s, x + 47 * s, y + 12 * s), light)


@lru_cache(maxsize=32)
def _font(path: str, size: int):
    return ImageFont.truetype(path, size)


def _font_path(style: dict, context: dict) -> str:
    configured = context.get("font_path") or style.get("subtitle", {}).get("font_path")
    candidates = [
        configured,
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(str(candidate)).is_file():
            return str(candidate)
    raise RuntimeError("像素场景标牌需要可用的中文字体")


def _sign(draw, box, text: str, colors: dict, font_path: str, size: int = 12) -> None:
    x0, y0, x1, y1 = box
    _rect(draw, (x0 + 2, y0 + 2, x1 + 2, y1 + 2), colors["wood_dark"])
    _rect(draw, box, colors["train_cream"], colors["ink"], 2)
    font = _font(font_path, size)
    bounds = draw.textbbox((0, 0), text, font=font)
    x = (x0 + x1 - (bounds[2] - bounds[0])) // 2 - bounds[0]
    y = (y0 + y1 - (bounds[3] - bounds[1])) // 2 - bounds[1]
    draw.text((x, y), text, font=font, fill=colors["ink"])


def _mix(first, second, amount: float):
    return tuple(int(round(a + (b - a) * amount)) for a, b in zip(first, second))


def _gradient(draw, box, stops) -> None:
    x0, y0, x1, y1 = box
    height = max(1, y1 - y0)
    for y in range(y0, y1 + 1):
        position = (y - y0) / height
        left = stops[0]
        right = stops[-1]
        for index in range(len(stops) - 1):
            if stops[index][0] <= position <= stops[index + 1][0]:
                left, right = stops[index], stops[index + 1]
                break
        span = max(1e-6, right[0] - left[0])
        color = _mix(left[1], right[1], (position - left[0]) / span)
        _line(draw, ((x0, y), (x1, y)), color)


def _sky(draw, colors: dict, weather: str, width: int = 480) -> None:
    if weather == "night":
        stops = ((0.0, colors["indigo"]), (0.65, colors["sky_shadow"]), (1.0, colors["sea_dark"]))
    elif weather == "rain":
        stops = ((0.0, colors["steel_dark"]), (0.55, colors["sky_shadow"]), (1.0, colors["steel_mid"]))
    elif weather == "sunset":
        stops = ((0.0, colors["sky_shadow"]), (0.42, colors["pink"]), (0.76, colors["coral"]), (1.0, colors["amber"]))
    else:
        stops = ((0.0, colors["sky"]), (0.62, colors["sky_light"]), (1.0, colors["glass_light"]))
    _gradient(draw, (0, 0, width, 128), stops)
    if weather == "night":
        for x, y in ((38, 24), (91, 51), (164, 19), (246, 42), (327, 22), (410, 53), (453, 17)):
            _rect(draw, (x, y, x + 2, y + 2), colors["cream"])
    elif weather == "rain":
        _cloud(draw, 35, 30, 1, colors["steel_mid"], colors["sky_shadow"])
        _cloud(draw, 330, 48, 1, colors["steel_mid"], colors["sky_shadow"])
    else:
        shade = colors["pink"] if weather == "sunset" else colors["glass_light"]
        _cloud(draw, 45, 32, 1, colors["warm_white"], shade)
        _cloud(draw, 335, 55, 1, colors["warm_white"], shade)


def _sea(draw, colors: dict, top: int, bottom: int, weather: str, frame: int = 0) -> None:
    if weather in ("night", "rain"):
        stops = ((0.0, colors["sky_shadow"]), (0.35, colors["sea_mid"]), (1.0, colors["sea_dark"]))
    else:
        stops = ((0.0, colors["sea_light"]), (0.42, colors["sea_mid"]), (1.0, colors["sea"]))
    _gradient(draw, (0, top, 480, bottom), stops)
    _rect(draw, (0, top, 480, top + 2), colors["glass_light"] if weather != "night" else colors["sky_shadow"])
    for row, y in enumerate(range(top + 9, bottom, 10)):
        offset = ((frame // 6 + row * 17) % 31) - 15
        for x in range(-35, 500, 52):
            length = 8 + (row + x) % 20
            color = colors["sea_light"] if (row + x) % 3 else colors["glass_light"]
            if weather in ("night", "rain"):
                color = colors["sky_shadow"] if (row + x) % 3 else colors["steel_mid"]
            _rect(draw, (x + offset, y, x + offset + length, y + 1), color)
            if row % 2 == 0:
                _rect(draw, (x + offset + 4, y + 3, x + offset + length - 3, y + 3), colors["sea_dark"])


def _island(draw, colors: dict, base_y: int, lighthouse: bool = False) -> None:
    draw.polygon(
        ((0, base_y), (0, base_y - 17), (18, base_y - 22), (30, base_y - 34),
         (52, base_y - 40), (76, base_y - 31), (96, base_y - 19), (132, base_y)),
        fill=colors["sea_dark"],
    )
    draw.polygon(
        ((0, base_y - 17), (20, base_y - 23), (33, base_y - 34), (54, base_y - 37),
         (79, base_y - 27), (92, base_y - 17), (112, base_y - 7), (0, base_y - 7)),
        fill=colors["green"],
    )
    for x, y in ((13, -20), (43, -31), (70, -24), (94, -12)):
        _rect(draw, (x, base_y + y, x + 8, base_y + y + 3), colors["leaf"])
    if lighthouse:
        _rect(draw, (28, base_y - 77, 39, base_y - 37), colors["train_cream"], colors["steel_dark"])
        for y in (base_y - 69, base_y - 53):
            _rect(draw, (29, y, 38, y + 6), colors["coral"])
        _rect(draw, (26, base_y - 81, 41, base_y - 76), colors["steel_dark"])
        _rect(draw, (31, base_y - 87, 36, base_y - 81), colors["signal_yellow"])
    else:
        _rect(draw, (76, base_y - 50, 103, base_y - 25), colors["train_cream"], colors["wood_dark"])
        draw.polygon(((71, base_y - 50), (89, base_y - 63), (108, base_y - 50)), fill=colors["coral"])
        _rect(draw, (84, base_y - 43, 94, base_y - 33), colors["glass_light"])


def _checker(draw, box, first, second, step: int = 4) -> None:
    x0, y0, x1, y1 = (int(value) for value in box)
    size = max(2, int(step))
    for y in range(y0, y1 + 1, size):
        for x in range(x0, x1 + 1, size):
            color = first if ((x - x0) // size + (y - y0) // size) % 2 else second
            _rect(draw, (x, y, min(x1, x + size - 1), min(y1, y + size - 1)), color)


def _platform(draw, colors: dict, top: int = 184, weather: str = "day", warm_rain: bool = True) -> None:
    _rect(draw, (0, 168, 480, top), colors["stone_dark"])
    for x in range(-8, 490, 24):
        _rect(draw, (x, 171, x + 14, 181), colors["wood_dark"])
    _rect(draw, (0, 169, 480, 172), colors["steel_light"])
    _rect(draw, (0, 180, 480, 182), colors["steel_mid"])

    base = colors["stone_light"]
    line = colors["stone_mid"]
    if weather == "rain" and warm_rain:
        base = colors["station_wet"]
        line = colors["station_wet_line"]
    elif weather == "rain":
        base = _mix(colors["stone_mid"], colors["sky_shadow"], 0.42)
        line = _mix(colors["stone_dark"], colors["sky_light"], 0.25)
    elif weather == "snow":
        base = _mix(colors["stone_light"], colors["sky_light"], 0.58)
        line = _mix(colors["stone_mid"], colors["glass_light"], 0.45)
    elif weather == "night":
        base = _mix(colors["stone_mid"], colors["violet"], 0.35)
    _rect(draw, (0, top, 480, 270), base)
    _rect(draw, (0, top, 480, top + 4), colors["train_cream"])
    _rect(draw, (0, top + 9, 480, top + 16), colors["signal_yellow"])
    for x in range(0, 480, 12):
        _rect(draw, (x, top + 9, x + 5, top + 10), colors["wood_dark"])
        _rect(draw, (x + 4, top + 14, x + 8, top + 15), colors["train_cream"])
    for row, y in enumerate(range(top + 24, 271, 18)):
        _line(draw, ((0, y), (480, y)), line)
        for x in range((row % 2) * 28, 480, 56):
            _line(draw, ((x, y - 17), (x, y)), line)


def _station_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    weather = shot.get("weather", "day")
    image = Image.new("RGB", (480, 270), colors["sky"])
    draw = ImageDraw.Draw(image)
    _sky(draw, colors, weather)
    _sea(draw, colors, 119, 166, weather)
    if shot.get("action") == "embrace" or (shot.get("shot_type") == "close" and weather in ("rain", "night")):
        _gradient(draw, (0, 0, 480, 128), ((0.0, colors["sky_shadow"]), (1.0, colors["violet"])))
        _rect(draw, (338, 37, 480, 121), colors["wood_dark"])
        _rect(draw, (346, 45, 480, 121), colors["house"])
        draw.polygon(((326, 45), (374, 7), (438, 7), (480, 42)), fill=colors["wood_dark"])
        _rect(draw, (407, 51, 448, 88), colors["train_cream"], colors["ink"], 2)
        _rect(draw, (413, 57, 442, 82), colors["glass_light"])
        _line(draw, ((427, 57), (427, 82)), colors["coral"], 2)
        _line(draw, ((413, 69), (442, 69)), colors["coral"], 2)
        for x in range(-6, 490, 15):
            draw.polygon(((x, 121), (x + 2, 84), (x + 7, 78), (x + 12, 84), (x + 14, 121)), fill=colors["wood_light"])
        _rect(draw, (0, 96, 480, 101), colors["wood"])
        _rect(draw, (0, 119, 480, 140), colors["green"])
        for index in range(72):
            x = (index * 37) % 480
            y = 120 + (index * 13) % 18
            _rect(draw, (x, y, x + 3, y + 3), colors["leaf"] if index % 3 else colors["pink"])
        _rect(draw, (126, 40, 171, 102), colors["wood_dark"], colors["ink"], 2)
        for y in range(49, 94, 9):
            _rect(draw, (133, y, 151, y + 2), colors["signal_yellow"])
            _rect(draw, (155, y, 164, y + 2), colors["mint"])
        for x in (111, 376):
            _rect(draw, (x, 0, x + 7, 207), colors["steel_dark"])
            _rect(draw, (x + 2, 0, x + 4, 207), colors["steel_mid"])
            _rect(draw, (x - 4, 201, x + 12, 207), colors["steel_dark"])
        return image
    _island(draw, colors, 123, lighthouse=True)
    if shot.get("action") != "train" or shot.get("split_part"):
        if shot.get("split_part"):
            _train(draw, colors, -30)
            _train(draw, colors, 286)
        else:
            _train(draw, colors, 72)
            _train(draw, colors, 388)

    _rect(draw, (0, 41, 480, 49), colors["steel_dark"])
    _rect(draw, (0, 49, 480, 53), colors["coral"])
    _rect(draw, (0, 41, 480, 43), colors["steel_mid"])
    for x in (108, 238, 368):
        _rect(draw, (x, 53, x + 7, 207), colors["steel_mid"])
        _rect(draw, (x + 2, 53, x + 4, 207), colors["steel_light"])
        _rect(draw, (x - 4, 201, x + 12, 207), colors["steel_dark"])

    sign_box = (150, 62, 206, 88) if shot.get("split_part") else (212, 62, 268, 88)
    _sign(draw, sign_box, "海边站", colors, font_path, 14)
    _rect(draw, (214, 96, 238, 150), colors["steel_dark"], colors["ink"], 2)
    for y in range(104, 143, 8):
        _rect(draw, (217, y, 227, y + 2), colors["signal_yellow"])
        _rect(draw, (230, y, 235, y + 2), colors["mint"])

    if weather in ("rain", "snow", "night"):
        _rect(draw, (275, 76, 382, 177), colors["steel_dark"], colors["ink"], 2)
        _rect(draw, (282, 83, 375, 169), colors["glass_light"])
        _checker(draw, (284, 85, 373, 167), colors["glass_light"], colors["sky_light"], 3)
        _rect(draw, (297, 91, 352, 143), colors["train_cream"], colors["wood_dark"], 2)
        _rect(draw, (305, 99, 344, 120), colors["coral"])
        _rect(draw, (315, 103, 334, 116), colors["amber"])
        _line(draw, ((308, 130), (340, 130)), colors["stone_mid"], 2)
        _line(draw, ((314, 136), (334, 136)), colors["stone_mid"], 2)
    else:
        draw.ellipse((413, 67, 437, 91), fill=colors["train_cream"], outline=colors["ink"], width=2)
        _line(draw, ((425, 70), (425, 79), (432, 82)), colors["coral"], 2)
    return image


def _station_foreground(image: Image.Image, shot: dict, colors: dict) -> None:
    draw = ImageDraw.Draw(image)
    weather = shot.get("weather", "day")
    _platform(draw, colors, weather=weather, warm_rain=not shot.get("split_part", False))
    close = shot.get("action") == "embrace" or shot.get("shot_type") == "close"
    for left in ((-32, 392) if close else (78, 326)):
        _rect(draw, (left, 194, left + 105, 199), colors["steel_dark"])
        for y in (196, 204, 212):
            _rect(draw, (left + 3, y, left + 101, y + 5), colors["wood_light"], colors["wood_dark"])
        for x in (left + 9, left + 91):
            _rect(draw, (x, 192, x + 4, 231), colors["steel_dark"])
            _rect(draw, (x - 4, 227, x + 10, 232), colors["steel_dark"])

    if close:
        _rect(draw, (309, 211, 341, 235), colors["coral"], colors["wood_dark"])
        for box in ((303, 200, 345, 214), (311, 192, 340, 207), (319, 187, 348, 204)):
            _rect(draw, box, colors["green"])
            _rect(draw, (box[0] + 6, box[1] + 3, box[0] + 12, box[1] + 6), colors["leaf"])
        if weather in ("rain", "night"):
            draw.polygon(((340, 216), (365, 202), (392, 209), (372, 219)), fill=colors["red"], outline=colors["ink"])
            _line(draw, ((366, 216), (385, 231), (391, 229)), colors["ink"], 2)
        return

    vending = "red" if weather in ("rain", "snow") else "blue"
    _rect(draw, (18, 153, 61, 226), colors[vending], colors["ink"], 2)
    _rect(draw, (24, 160, 54, 195), colors["steel_dark"])
    for row in range(3):
        for column in range(4):
            color = ("coral", "amber", "mint", "glass_light")[(row + column) % 4]
            _rect(draw, (27 + column * 7, 164 + row * 9, 31 + column * 7, 171 + row * 9), colors[color])
    _rect(draw, (25, 202, 53, 207), colors["glass_light"])
    _rect(draw, (28, 213, 49, 222), colors["ink"])
    _rect(draw, (427, 153, 470, 226), colors["red"] if weather == "rain" else colors[vending], colors["ink"], 2)
    _rect(draw, (433, 160, 463, 195), colors["steel_dark"])
    for row in range(3):
        for column in range(4):
            color = ("mint", "coral", "glass_light", "amber")[(row + column) % 4]
            _rect(draw, (436 + column * 7, 164 + row * 9, 440 + column * 7, 171 + row * 9), colors[color])
    _rect(draw, (436, 213, 458, 222), colors["ink"])

    for x in (73, 405):
        _rect(draw, (x, 163, x + 4, 224), colors["steel_dark"])
        _rect(draw, (x - 7, 158, x + 11, 164), colors["steel_dark"])
        _rect(draw, (x - 4, 164, x + 8, 174), colors["train_cream"])
    _rect(draw, (284, 211, 316, 235), colors["coral"], colors["wood_dark"])
    for box in ((278, 200, 320, 214), (286, 192, 315, 207), (294, 187, 323, 204)):
        _rect(draw, box, colors["green"])
        _rect(draw, (box[0] + 6, box[1] + 3, box[0] + 12, box[1] + 6), colors["leaf"])


def _seaside_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    weather = shot.get("weather", "sunset")
    image = Image.new("RGB", (480, 270), colors["sky"])
    draw = ImageDraw.Draw(image)
    _sky(draw, colors, weather)
    if weather != "night":
        _rect(draw, (354, 63, 391, 100), colors["sun"] if weather == "sunset" else colors["warm_white"])
        _rect(draw, (350, 72, 395, 91), colors["sun"] if weather == "sunset" else colors["warm_white"])
    _sea(draw, colors, 126, 213, weather)
    _island(draw, colors, 132)
    _rect(draw, (0, 207, 480, 270), colors["sand"])
    for y in range(215, 270, 11):
        for x in range((y % 4) * 6, 480, 28):
            _rect(draw, (x, y, x + 6, y + 2), colors["wood_light"])
    for x in range(0, 481, 60):
        _rect(draw, (x, 168, x + 5, 218), colors["wood_dark"])
        _rect(draw, (x + 1, 168, x + 3, 216), colors["wood_light"])
    _rect(draw, (0, 174, 480, 180), colors["wood_dark"])
    _rect(draw, (0, 175, 480, 177), colors["wood_light"])
    return image


def _bedroom_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    weather = shot.get("weather", "night")
    image = Image.new("RGB", (480, 270), colors["cream"])
    draw = ImageDraw.Draw(image)
    _rect(draw, (0, 0, 480, 187), colors["cream"])
    for x in range(0, 480, 24):
        _rect(draw, (x, 0, x + 2, 187), colors["wood_light"])
    _rect(draw, (0, 184, 480, 270), colors["wood"])
    for y in range(190, 270, 12):
        _line(draw, ((0, y), (480, y)), colors["wood_dark"])
    _rect(draw, (274, 27, 451, 144), colors["wood_dark"])
    _rect(draw, (282, 35, 443, 136), colors["sky_shadow"] if weather == "night" else colors["sky_light"])
    _rect(draw, (282, 101, 443, 136), colors["sea_mid"])
    _rect(draw, (359, 35, 365, 136), colors["wood_dark"])
    _rect(draw, (282, 82, 443, 87), colors["wood_dark"])
    if weather == "night":
        _rect(draw, (405, 48, 421, 64), colors["cream"])
    _rect(draw, (266, 23, 278, 153), colors["coral"])
    _rect(draw, (448, 23, 460, 153), colors["coral"])

    _rect(draw, (24, 150, 235, 213), colors["wood_dark"])
    _rect(draw, (31, 143, 228, 196), colors["mint"])
    _rect(draw, (37, 148, 109, 171), colors["warm_white"])
    _rect(draw, (31, 184, 228, 199), colors["green"])
    for x in range(42, 220, 29):
        _rect(draw, (x, 188, x + 12, 191), colors["leaf"])
    _rect(draw, (30, 213, 38, 242), colors["wood_dark"])
    _rect(draw, (218, 213, 226, 242), colors["wood_dark"])

    _rect(draw, (320, 165, 465, 177), colors["wood_dark"])
    _rect(draw, (328, 177, 335, 231), colors["wood_dark"])
    _rect(draw, (447, 177, 454, 231), colors["wood_dark"])
    _rect(draw, (397, 150, 413, 165), colors["amber"])
    draw.polygon(((386, 150), (425, 150), (416, 126), (395, 126)), fill=colors["signal_yellow"])
    _rect(draw, (404, 116, 407, 127), colors["wood_dark"])
    for index, color in enumerate(("coral", "sky", "cream", "leaf")):
        _rect(draw, (337 + index * 10, 146 - index % 2 * 4, 344 + index * 10, 164), colors[color])
    _rect(draw, (42, 39, 126, 105), colors["wood_dark"])
    _rect(draw, (49, 46, 119, 98), colors["sky"])
    draw.polygon(((52, 94), (78, 64), (97, 87), (116, 61), (116, 95)), fill=colors["sea_dark"])
    return image


def _rain_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    image = Image.new("RGB", (480, 270), colors["indigo"])
    draw = ImageDraw.Draw(image)
    _sky(draw, colors, "rain")
    buildings = ((0, 70, 80), (75, 112, 83), (165, 54, 82), (251, 91, 108), (365, 45, 115))
    for index, (x, top, width) in enumerate(buildings):
        _rect(draw, (x, top, x + width, 184), colors["steel_dark"] if index % 2 else colors["sky_shadow"])
        for wx in range(x + 10, x + width - 5, 22):
            for wy in range(top + 12, 172, 25):
                color = colors["amber"] if (wx + wy) % 4 else colors["steel_mid"]
                _rect(draw, (wx, wy, wx + 8, wy + 12), color)
                _line(draw, ((wx, wy + 5), (wx + 8, wy + 5)), colors["wood_dark"])
    _rect(draw, (0, 181, 480, 270), colors["steel_mid"])
    _rect(draw, (0, 205, 480, 270), colors["indigo"])
    for x, color, width in ((45, "amber", 22), (137, "coral", 17), (277, "sky", 26), (399, "pink", 19)):
        _rect(draw, (x, 209, x + width, 215), colors[color])
        _rect(draw, (x + 4, 220, x + width - 4, 222), colors[color])
        _rect(draw, (x + 8, 227, x + width - 8, 229), colors[color])
    for x in (61, 382):
        _rect(draw, (x, 77, x + 5, 199), colors["ink"])
        _rect(draw, (x - 9, 75, x + 14, 81), colors["steel_dark"])
        _rect(draw, (x - 5, 81, x + 10, 97), colors["amber"])
    return image


def _campus_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    if shot.get("shot_type") in ("close", "object"):
        image = _bedroom_background(shot, colors, font_path)
        draw = ImageDraw.Draw(image)
        _rect(draw, (22, 25, 242, 119), colors["wood_dark"])
        _rect(draw, (29, 32, 235, 111), colors["green"])
        _sign(draw, (70, 43, 194, 73), "毕业纪念", colors, font_path, 13)
        for x in (75, 175, 275, 375):
            _rect(draw, (x, 169, x + 65, 179), colors["wood_light"])
            _rect(draw, (x + 6, 179, x + 10, 222), colors["steel_dark"])
            _rect(draw, (x + 54, 179, x + 58, 222), colors["steel_dark"])
        return image
    image = Image.new("RGB", (480, 270), colors["sky"])
    draw = ImageDraw.Draw(image)
    _sky(draw, colors, shot.get("weather", "day"))
    _rect(draw, (0, 173, 480, 270), colors["leaf"])
    _rect(draw, (66, 66, 416, 181), colors["train_cream"], colors["wood_dark"], 2)
    _rect(draw, (57, 58, 425, 72), colors["coral"])
    _rect(draw, (209, 35, 273, 61), colors["train_cream"])
    _rect(draw, (204, 29, 278, 38), colors["coral"])
    for y in (84, 124):
        for x in range(84, 401, 42):
            _rect(draw, (x, y, x + 26, y + 24), colors["wood_dark"])
            _rect(draw, (x + 3, y + 3, x + 23, y + 20), colors["glass_light"])
            _line(draw, ((x + 13, y + 3), (x + 13, y + 20)), colors["sky_shadow"])
    _rect(draw, (219, 143, 263, 181), colors["wood"])
    _rect(draw, (0, 201, 480, 270), colors["coral"])
    for y in (213, 234, 255):
        _line(draw, ((0, y), (480, y)), colors["cream"], 2)
    for x in (30, 444):
        _rect(draw, (x - 3, 122, x + 4, 196), colors["wood_dark"])
        for box in ((x - 25, 94, x + 8, 143), (x - 7, 82, x + 26, 137), (x - 19, 73, x + 13, 117)):
            _rect(draw, box, colors["green"])
            _rect(draw, (box[0] + 6, box[1] + 6, box[0] + 12, box[1] + 12), colors["leaf"])
    return image


def _highway_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    image = Image.new("RGB", (480, 270), colors["sky"])
    draw = ImageDraw.Draw(image)
    _sky(draw, colors, shot.get("weather", "sunset"))
    _sea(draw, colors, 124, 172, shot.get("weather", "day"))
    draw.polygon(((0, 155), (85, 105), (163, 154), (240, 116), (324, 158), (407, 104), (480, 151), (480, 183), (0, 183)), fill=colors["sea_dark"])
    _rect(draw, (0, 166, 480, 270), colors["sand"])
    draw.polygon(((205, 156), (275, 156), (403, 270), (66, 270)), fill=colors["steel_dark"])
    _line(draw, ((205, 156), (66, 270)), colors["cream"], 3)
    _line(draw, ((275, 156), (403, 270)), colors["cream"], 3)
    for index in range(6):
        y = 163 + index * 21
        x = 240 + (y - 160) // 5
        _rect(draw, (x, y, x + 3 + index // 2, y + 8 + index * 2), colors["cream"])
    _rect(draw, (31, 153, 145, 207), colors["coral"], colors["ink"], 2)
    _rect(draw, (39, 159, 132, 181), colors["glass_light"])
    for x in (45, 75, 105):
        _rect(draw, (x, 162, x + 23, 179), colors["sky_shadow"])
    _rect(draw, (44, 189, 69, 195), colors["train_cream"])
    _rect(draw, (113, 188, 133, 195), colors["signal_yellow"])
    _rect(draw, (46, 204, 65, 213), colors["ink"])
    _rect(draw, (112, 204, 131, 213), colors["ink"])
    return image


def _stage_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    image = Image.new("RGB", (480, 270), colors["wood_dark"])
    draw = ImageDraw.Draw(image)
    _rect(draw, (22, 18, 458, 203), colors["indigo"])
    _rect(draw, (22, 18, 458, 29), colors["wood"])
    for x in range(35, 450, 27):
        _rect(draw, (x, 34, x + 5, 39), colors["amber"])
        _line(draw, ((x + 2, 29), (x + 2, 34)), colors["ink"])
    for x in (22, 431):
        _rect(draw, (x, 29, x + 27, 203), colors["red"])
        for dx in range(4, 25, 7):
            _line(draw, ((x + dx, 31), (x + dx, 199)), colors["coral"], 2)
    _rect(draw, (0, 196, 480, 270), colors["wood"])
    for y in range(202, 270, 12):
        _line(draw, ((0, y), (480, y)), colors["wood_dark"])
    _rect(draw, (71, 64, 151, 115), colors["steel_dark"])
    _rect(draw, (80, 73, 142, 105), colors["ink"])
    _rect(draw, (88, 80, 134, 99), colors["steel_mid"])
    _rect(draw, (348, 118, 363, 198), colors["ink"])
    _rect(draw, (332, 196, 380, 201), colors["ink"])
    return image


def _ancient_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    image = Image.new("RGB", (480, 270), colors["indigo"])
    draw = ImageDraw.Draw(image)
    _sky(draw, colors, "night")
    _rect(draw, (371, 32, 405, 66), colors["cream"])
    draw.polygon(((0, 162), (70, 81), (145, 159), (225, 66), (322, 163), (403, 91), (480, 157), (480, 199), (0, 199)), fill=colors["sky_shadow"])
    draw.polygon(((0, 184), (105, 127), (193, 183), (287, 122), (394, 186), (480, 143), (480, 211), (0, 211)), fill=colors["sea_dark"])
    _rect(draw, (0, 199, 480, 270), colors["cream"])
    _rect(draw, (92, 132, 390, 204), colors["wood_dark"])
    _rect(draw, (104, 142, 378, 200), colors["train_cream"])
    draw.polygon(((64, 137), (116, 107), (368, 107), (420, 137)), fill=colors["ink"])
    _rect(draw, (75, 136, 409, 143), colors["steel_mid"])
    for x in (121, 207, 293, 367):
        _rect(draw, (x, 142, x + 9, 204), colors["red"])
    for x in (155, 327):
        _rect(draw, (x, 153, x + 17, 175), colors["red"])
        _rect(draw, (x + 5, 175, x + 12, 184), colors["amber"])
    return image


def _generic_background(shot: dict, colors: dict, font_path: str) -> Image.Image:
    image = _bedroom_background(shot, colors, font_path)
    draw = ImageDraw.Draw(image)
    _rect(draw, (23, 22, 243, 135), colors["wood_dark"])
    for y in (29, 62, 95):
        _rect(draw, (30, y, 236, y + 25), colors["wood"])
        for x in range(35, 224, 24):
            color = ("coral", "sky", "cream", "green")[(x + y) % 4]
            _rect(draw, (x, y + 3, x + 18, y + 21), colors[color])
            _rect(draw, (x + 5, y + 7, x + 13, y + 17), colors["ink"])
    _sign(draw, (56, 106, 213, 132), "旧唱片与信", colors, font_path, 11)
    return image


_BACKGROUND = {
    "station": _station_background,
    "seaside": _seaside_background,
    "bedroom": _bedroom_background,
    "rain_street": _rain_background,
    "campus": _campus_background,
    "highway": _highway_background,
    "stage": _stage_background,
    "ancient": _ancient_background,
    "generic": _generic_background,
}


def _train(draw, colors: dict, x: int, y: int = 104) -> None:
    _rect(draw, (x + 5, y + 4, x + 322, y + 69), colors["steel_dark"])
    _rect(draw, (x, y, x + 316, y + 58), colors["train_cream"], colors["ink"], 2)
    _rect(draw, (x, y + 36, x + 316, y + 41), colors["blue"])
    _rect(draw, (x, y + 43, x + 316, y + 45), colors["sea"])
    _checker(draw, (x + 3, y + 47, x + 313, y + 55), colors["train_cream"], colors["stone_light"], 3)
    _rect(draw, (x + 4, y + 4, x + 310, y + 8), colors["warm_white"])
    for index, wx in enumerate(range(x + 16, x + 278, 47)):
        _rect(draw, (wx, y + 12, wx + 34, y + 34), colors["steel_dark"])
        _rect(draw, (wx + 3, y + 14, wx + 31, y + 31), colors["sky"])
        _line(draw, ((wx + 6, y + 16), (wx + 20, y + 16)), colors["glass_light"], 2)
        if index % 2:
            _rect(draw, (wx + 9, y + 25, wx + 14, y + 31), colors["pink"])
    _rect(draw, (x + 285, y + 10, x + 303, y + 50), colors["wood_dark"])
    _rect(draw, (x + 289, y + 14, x + 299, y + 32), colors["glass_light"])
    for wx in (x + 42, x + 243):
        _rect(draw, (wx, y + 54, wx + 38, y + 64), colors["ink"])
        _rect(draw, (wx + 7, y + 56, wx + 14, y + 62), colors["steel_mid"])
        _rect(draw, (wx + 25, y + 56, wx + 32, y + 62), colors["steel_mid"])


def _character(
    draw,
    colors: dict,
    role: str,
    x: int,
    ground: int,
    height: int,
    pose: int,
    blink: bool,
    action: str,
    facing: int = 0,
) -> None:
    companion = role == "companion"
    s = height / 100.0
    direction = 1 if facing >= 0 else -1
    ink = colors["ink"]
    skin = colors["skin"]
    hair = colors["hair"] if not companion else colors["wood_dark"]
    coat = colors["blue"] if not companion else colors["train_cream"]
    trousers = colors["indigo"] if not companion else colors["red"]

    def relative(values):
        return (x + int(values[0] * s), ground + int(values[1] * s),
                x + int(values[2] * s), ground + int(values[3] * s))

    def oriented(values):
        x0 = x + int(values[0] * s * direction)
        x1 = x + int(values[2] * s * direction)
        return (min(x0, x1), ground + int(values[1] * s),
                max(x0, x1), ground + int(values[3] * s))

    def points(values):
        return tuple((x + int(px * s), ground + int(py * s)) for px, py in values)

    def opoints(values):
        return tuple((x + int(px * s * direction), ground + int(py * s)) for px, py in values)

    if companion:
        if facing:
            _rect(draw, oriented((-30, -91, -17, -42)), hair, ink)
        else:
            draw.polygon(points(((-27, -91), (-18, -99), (19, -98), (29, -88),
                                 (30, -44), (18, -39), (-23, -47), (-30, -72))), fill=hair, outline=ink)

    step = 3 if pose and action == "walking" else 0
    _rect(draw, relative((-12 - step, -28, -2 - step, -4)), trousers, ink)
    _rect(draw, relative((3 + step, -28, 13 + step, -4)), trousers, ink)
    if companion:
        draw.polygon(points(((-18, -40), (18, -40), (24, -20), (13, -16),
                             (-14, -16), (-24, -20))), fill=colors["red"], outline=ink)
        _rect(draw, relative((-20, -24, 20, -20)), colors["coral"])
    _rect(draw, relative((-16 - step, -6, -1 - step, 0)), ink)
    _rect(draw, relative((2 + step, -6, 17 + step, 0)), ink)
    if height >= 70:
        for sx in (-13, 5):
            _rect(draw, relative((sx, -5, sx + 3, -3)), colors["train_cream"])
            _rect(draw, relative((sx + 5, -3, sx + 8, -1)), colors["steel_light"])

    draw.polygon(points(((-15, -62), (15, -62), (21, -53), (18, -27),
                         (8, -23), (-9, -23), (-19, -28), (-21, -53))), fill=coat, outline=ink)
    _rect(draw, relative((-13, -61, 13, -54)), colors["warm_white"] if companion else colors["red"])
    _rect(draw, relative((-4, -53, 4, -27)), colors["warm_white"])
    _rect(draw, relative((-14, -36, -6, -30)), coat, ink)
    _rect(draw, relative((7, -36, 14, -30)), coat, ink)

    if facing and not companion:
        _rect(draw, oriented((-30, -58, -19, -29)), colors["wood"], ink)
    if action == "embrace":
        _rect(draw, oriented((2, -68, 33, -59)), coat, ink)
        _rect(draw, oriented((28, -65, 38, -57)), skin, ink)
        _rect(draw, oriented((-15, -45, 23, -37)), coat, ink)
        _rect(draw, oriented((18, -44, 27, -36)), skin, ink)
    elif facing and action in ("talking", "reading", "walking", "playing", "cycling", "photographing"):
        _rect(draw, oriented((8, -56, 29, -47)), coat, ink)
        _rect(draw, oriented((26, -51, 34, -43)), skin, ink)
        _rect(draw, oriented((-25, -54, -16, -30)), coat, ink)
    else:
        _rect(draw, relative((-26, -55, -17, -30)), coat, ink)
        _rect(draw, relative((17, -55, 26, -30)), coat, ink)
        _rect(draw, relative((-24, -34, -17, -27)), skin, ink)
        _rect(draw, relative((17, -34, 24, -27)), skin, ink)

    _rect(draw, relative((-6, -68, 6, -57)), skin, ink)
    if facing:
        draw.polygon(opoints(((-26, -94), (-17, -100), (16, -98), (25, -90),
                              (25, -75), (18, -62), (-12, -59), (-25, -69))), fill=ink)
        draw.polygon(opoints(((-20, -91), (10, -92), (19, -85), (19, -79),
                              (29, -75), (20, -70), (16, -63), (-11, -63),
                              (-21, -71))), fill=skin)
        draw.polygon(opoints(((-25, -92), (-18, -99), (16, -97), (22, -90),
                              (13, -85), (6, -90), (-2, -84), (-12, -87),
                              (-18, -76), (-24, -72))), fill=hair)
        _rect(draw, oriented((-17, -96, -7, -93)), colors["wood_light"] if companion else colors["steel_mid"])
        eye_height = 1 if blink else 5
        _rect(draw, oriented((10, -80, 14, -80 + eye_height)), ink)
        _rect(draw, oriented((13, -70, 18, -67)), colors["pink"])
        _rect(draw, oriented((20, -67, 25, -64)), colors["wood_dark"])
        if companion:
            _rect(draw, oriented((-26, -80, -19, -48)), hair)
            _rect(draw, oriented((3, -100, 12, -94)), colors["coral"], ink)
    else:
        draw.polygon(points(((-21, -98), (19, -98), (28, -90), (28, -75),
                             (21, -64), (11, -59), (-11, -59), (-23, -65),
                             (-29, -77), (-27, -91))), fill=ink)
        draw.polygon(points(((-19, -93), (18, -93), (23, -87), (23, -75),
                             (17, -67), (8, -63), (-10, -63), (-19, -68),
                             (-24, -77), (-23, -88))), fill=skin)
        draw.polygon(points(((-26, -91), (-19, -99), (18, -99), (26, -91),
                             (25, -80), (19, -76), (16, -87), (8, -82),
                             (0, -89), (-8, -82), (-18, -86), (-22, -74),
                             (-27, -77))), fill=hair)
        _rect(draw, relative((-17, -96, -7, -93)), colors["wood_light"] if companion else colors["steel_mid"])
        eye_height = 1 if blink else 5
        _rect(draw, relative((-14, -79, -10, -79 + eye_height)), ink)
        _rect(draw, relative((10, -79, 14, -79 + eye_height)), ink)
        _rect(draw, relative((-20, -70, -15, -67)), colors["pink"])
        _rect(draw, relative((15, -70, 20, -67)), colors["pink"])
        _rect(draw, relative((-2, -69, 2, -66)), colors["wood_light"])
        _rect(draw, relative((-4, -64, 4, -62)), colors["wood_dark"])
        if companion:
            _rect(draw, relative((-28, -79, -22, -48)), hair)
            _rect(draw, relative((22, -79, 28, -47)), hair)
            _rect(draw, relative((17, -96, 27, -89)), colors["coral"], ink)


def _cat(draw, colors: dict, x: int, ground: int, scale: int = 1) -> None:
    s = max(1, scale)
    _rect(draw, (x - 12 * s, ground - 15 * s, x + 9 * s, ground - 3 * s), colors["amber"], colors["ink"], s)
    _rect(draw, (x - 10 * s, ground - 26 * s, x + 7 * s, ground - 12 * s), colors["amber"], colors["ink"], s)
    draw.polygon(((x - 10 * s, ground - 24 * s), (x - 8 * s, ground - 34 * s), (x - 2 * s, ground - 25 * s)), fill=colors["amber"])
    draw.polygon(((x + 1 * s, ground - 25 * s), (x + 7 * s, ground - 34 * s), (x + 8 * s, ground - 23 * s)), fill=colors["amber"])
    _rect(draw, (x - 5 * s, ground - 20 * s, x - 3 * s, ground - 17 * s), colors["ink"])
    _rect(draw, (x + 2 * s, ground - 20 * s, x + 4 * s, ground - 17 * s), colors["ink"])
    _rect(draw, (x - 1 * s, ground - 14 * s, x + 2 * s, ground - 12 * s), colors["pink"])
    _rect(draw, (x + 8 * s, ground - 24 * s, x + 12 * s, ground - 5 * s), colors["amber"], colors["ink"], s)
    _rect(draw, (x + 10 * s, ground - 27 * s, x + 17 * s, ground - 22 * s), colors["amber"])


def _bicycle(draw, colors: dict, x: int, ground: int, scale: int = 1) -> None:
    s = max(1, scale)
    for wheel_x in (x - 23 * s, x + 23 * s):
        draw.ellipse((wheel_x - 13 * s, ground - 26 * s, wheel_x + 13 * s, ground), outline=colors["ink"], width=2 * s)
        _rect(draw, (wheel_x - s, ground - 14 * s, wheel_x + s, ground - 12 * s), colors["steel_light"])
    crank = (x, ground - 12 * s)
    _line(draw, ((x - 23 * s, ground - 13 * s), (x - 7 * s, ground - 34 * s), crank, (x + 14 * s, ground - 34 * s), (x + 23 * s, ground - 13 * s)), colors["coral"], 2 * s)
    _line(draw, ((x - 7 * s, ground - 34 * s), (x + 14 * s, ground - 34 * s), crank, (x - 23 * s, ground - 13 * s)), colors["coral"], 2 * s)
    _line(draw, ((x + 14 * s, ground - 34 * s), (x + 10 * s, ground - 43 * s), (x + 20 * s, ground - 43 * s)), colors["ink"], 2 * s)
    _rect(draw, (x - 13 * s, ground - 38 * s, x - 2 * s, ground - 35 * s), colors["ink"])


def _object(draw, colors: dict, name: str, x: int, y: int, scale: int = 2) -> None:
    s = max(1, scale)
    if name == "cat":
        _cat(draw, colors, x, y + 28 * s, s)
        return
    if name in ("letter", "ticket"):
        _rect(draw, (x - 19 * s, y - 12 * s, x + 19 * s, y + 12 * s), colors["warm_white"], colors["wood_dark"], s)
        _line(draw, ((x - 18 * s, y - 11 * s), (x, y + 2 * s), (x + 18 * s, y - 11 * s)), colors["wood"], s)
        _rect(draw, (x + 10 * s, y + 4 * s, x + 15 * s, y + 9 * s), colors["red"])
    elif name == "photo":
        _rect(draw, (x - 18 * s, y - 22 * s, x + 18 * s, y + 20 * s), colors["wood_dark"])
        _rect(draw, (x - 14 * s, y - 18 * s, x + 14 * s, y + 12 * s), colors["sky"])
        _rect(draw, (x - 14 * s, y + 12 * s, x + 14 * s, y + 17 * s), colors["warm_white"])
        draw.polygon(((x - 12 * s, y + 10 * s), (x - 2 * s, y - 5 * s), (x + 11 * s, y + 10 * s)), fill=colors["sea_dark"])
        _rect(draw, (x + 5 * s, y - 13 * s, x + 10 * s, y - 8 * s), colors["amber"])
    elif name == "bicycle":
        _bicycle(draw, colors, x, y + 18 * s, s)
    elif name == "book":
        draw.polygon(((x - 23 * s, y - 15 * s), (x - s, y - 10 * s), (x - s, y + 15 * s), (x - 23 * s, y + 10 * s)), fill=colors["warm_white"], outline=colors["wood_dark"])
        draw.polygon(((x + 23 * s, y - 15 * s), (x + s, y - 10 * s), (x + s, y + 15 * s), (x + 23 * s, y + 10 * s)), fill=colors["train_cream"], outline=colors["wood_dark"])
        for offset in (-6, 0, 6):
            _line(draw, ((x - 18 * s, y + offset * s - 5 * s), (x - 5 * s, y + offset * s - 2 * s)), colors["steel_mid"], s)
            _line(draw, ((x + 5 * s, y + offset * s - 2 * s), (x + 18 * s, y + offset * s - 5 * s)), colors["steel_mid"], s)
    elif name in ("phone", "camera"):
        _rect(draw, (x - 15 * s, y - 20 * s, x + 15 * s, y + 20 * s), colors["steel_dark"], colors["ink"], s)
        _rect(draw, (x - 11 * s, y - 15 * s, x + 11 * s, y + 10 * s), colors["sky_shadow"])
        _rect(draw, (x - 8 * s, y - 11 * s, x + 7 * s, y - 7 * s), colors["glass_light"])
        _rect(draw, (x - 2 * s, y + 14 * s, x + 2 * s, y + 16 * s), colors["cream"])
        if name == "camera":
            draw.ellipse((x - 8 * s, y - 8 * s, x + 8 * s, y + 8 * s), fill=colors["ink"], outline=colors["glass_light"], width=s)
    elif name == "guitar":
        _rect(draw, (x - 4 * s, y - 34 * s, x + 4 * s, y - 6 * s), colors["wood_dark"])
        _rect(draw, (x - 12 * s, y - 10 * s, x + 12 * s, y + 14 * s), colors["amber"], colors["wood_dark"], s)
        _rect(draw, (x - 4 * s, y - 2 * s, x + 4 * s, y + 5 * s), colors["ink"])
        _line(draw, ((x, y - 32 * s), (x, y + 12 * s)), colors["cream"])
    elif name == "suitcase":
        _rect(draw, (x - 17 * s, y - 17 * s, x + 17 * s, y + 18 * s), colors["coral"], colors["ink"], s)
        _rect(draw, (x - 7 * s, y - 25 * s, x + 7 * s, y - 18 * s), colors["ink"])
        _rect(draw, (x - 9 * s, y - 12 * s, x - 5 * s, y + 13 * s), colors["amber"])
        _rect(draw, (x + 6 * s, y - 12 * s, x + 10 * s, y + 13 * s), colors["amber"])
    elif name == "red_umbrella":
        draw.polygon(((x - 27 * s, y - 6 * s), (x - 18 * s, y - 20 * s), (x, y - 28 * s), (x + 18 * s, y - 20 * s), (x + 27 * s, y - 6 * s)), fill=colors["red"])
        _rect(draw, (x - 27 * s, y - 7 * s, x + 27 * s, y - 3 * s), colors["coral"])
        _rect(draw, (x - s, y - 5 * s, x + s, y + 25 * s), colors["ink"])
    else:
        _rect(draw, (x - 17 * s, y - 16 * s, x + 17 * s, y + 16 * s), colors["steel_mid"], colors["ink"], s)
        _rect(draw, (x - 12 * s, y - 11 * s, x + 12 * s, y + 4 * s), colors["ink"])
        _rect(draw, (x - 7 * s, y - 7 * s, x + 7 * s, y + 1 * s), colors["sky"])


def _heart_glass(image: Image.Image, colors: dict, local_frame: int) -> None:
    draw = ImageDraw.Draw(image)
    fog_top = colors["fog"]
    fog_bottom = colors["fog_light"]
    _gradient(draw, (0, 0, 480, 136), ((0.0, fog_top), (1.0, fog_bottom)))
    _rect(draw, (0, 136, 480, 193), colors["fog_sea"])
    _rect(draw, (0, 194, 480, 270), colors["fog_ground"])
    _checker(draw, (0, 0, 479, 131), fog_top, fog_bottom, 3)
    _rect(draw, (0, 132, 480, 137), colors["steel_mid"])
    _rect(draw, (137, 0, 143, 230), colors["steel_mid"])
    _rect(draw, (140, 0, 143, 230), colors["glass_light"])
    for index in range(24):
        x = (index * 83 + local_frame) % 480
        y = (index * 47 + local_frame * 2) % 225
        _line(draw, ((x, y), (x - 1, y + 5)), colors["glass_light"])
    for index in range(48):
        angle = math.tau * index / 48
        px = 240 + int((math.sin(angle) ** 3) * 16 * 5.2)
        heart_y = 13 * math.cos(angle) - 5 * math.cos(2 * angle) - 2 * math.cos(3 * angle) - math.cos(4 * angle)
        py = 115 - int(heart_y * 5.8)
        if index > min(47, 8 + local_frame * 2):
            continue
        size = 5 + index % 4
        color = colors["sea_dark"] if py < 125 else _mix(colors["sea_dark"], colors["coral"], 0.45)
        _rect(draw, (px, py, px + size, py + size), color)

    hand_offset = 1 if local_frame // 6 % 2 else 0
    _rect(draw, (307, 202 + hand_offset, 352, 270), colors["blue"], colors["ink"], 2)
    draw.polygon(
        ((309, 204 + hand_offset), (309, 170 + hand_offset), (317, 164 + hand_offset),
         (320, 135 + hand_offset), (326, 127 + hand_offset), (333, 132 + hand_offset),
         (333, 161 + hand_offset), (340, 157 + hand_offset), (347, 163 + hand_offset),
         (352, 176 + hand_offset), (351, 204 + hand_offset)),
        fill=colors["skin"], outline=colors["ink"],
    )
    _rect(draw, (321, 136 + hand_offset, 329, 177 + hand_offset), colors["skin"])
    _line(draw, ((335, 174 + hand_offset), (346, 177 + hand_offset)), colors["wood_light"], 2)


def _actors_and_props(image: Image.Image, shot: dict, colors: dict, local_frame: int) -> None:
    draw = ImageDraw.Draw(image)
    characters = list(shot.get("characters", []))
    props = list(shot.get("props", []))
    shot_type = shot.get("shot_type", "wide")
    action = shot.get("action", "waiting")
    if action == "cycling":
        characters = characters[:1]
    hold = max(3, int(shot.get("pose_hold_frames", 6)))
    pose = (local_frame // hold) % 2
    blink = local_frame % 53 in (0, 1)

    if action == "heart_glass":
        _heart_glass(image, colors, local_frame)
        return
    if shot_type == "object":
        _rect(draw, (0, 184, 480, 270), colors["wood_dark"])
        _rect(draw, (0, 184, 480, 192), colors["wood_light"])
        count = min(2, len(props))
        for index, name in enumerate(props[:count]):
            x = 480 * (index + 1) // (count + 1)
            _rect(draw, (x - 35, 181, x + 35, 185), colors["ink"])
            _object(draw, colors, name, x, 153, 2)
        return

    height = 50 if shot_type in ("wide", "landscape") else 74
    if shot_type == "close":
        height = 104
    if action == "cycling":
        height = min(height, 64)
    if action == "embrace":
        height = min(height, 82)
        positions = ((222, 235), (258, 235))
    elif len(characters) == 1:
        positions = ((240, 217 if action == "cycling" else 228),)
    elif shot_type == "close":
        positions = ((185, 236), (295, 236))
    else:
        positions = ((205, 226), (275, 226))
    if action == "cycling" and characters:
        travel = int((local_frame * 2) % 560) - 40
        positions = ((travel, positions[0][1]),)
        _bicycle(draw, colors, travel, 239, 1)
    elif action == "walking" and len(characters) == 1:
        positions = ((70 + int(local_frame * 1.5) % 340, positions[0][1]),)
    idle = pose if action in ("waiting", "talking", "reading", "playing") else 0
    for index, role in enumerate(characters[:2]):
        x, ground = positions[index]
        facing = 0
        if len(characters) > 1 and action in ("talking", "embrace"):
            facing = 1 if index == 0 else -1
        elif action in ("walking", "cycling", "reading", "playing", "photographing"):
            facing = 1
        _character(draw, colors, role, x, ground + idle, height, (pose + index) % 2, blink, action, facing)
        if action == "reading":
            _object(draw, colors, "book", x + 20, ground - 42, 1)
        elif action == "playing":
            _object(draw, colors, "guitar", x + 24, ground - 20, 1)
        elif action == "photographing":
            _object(draw, colors, "camera", x + 27, ground - 57, 1)
    if action == "embrace" and shot.get("scene") == "station" and "cat" not in props:
        _cat(draw, colors, 158, 235, 1)
    for index, name in enumerate(props[:2]):
        if name == "bicycle" and action == "cycling":
            continue
        if name == "cat":
            cat_x = 205 if shot.get("compare_weather") else 350
            _cat(draw, colors, cat_x - index * 55, 235, 1)
        elif name != "red_umbrella" and shot_type != "landscape":
            _object(draw, colors, name, 66 + index * 48, 213, 1)
    if "red_umbrella" in props and shot_type != "landscape":
        _object(draw, colors, "red_umbrella", 242, 139, 2)


def _ambient(image: Image.Image, shot: dict, colors: dict, local_frame: int) -> None:
    draw = ImageDraw.Draw(image)
    scene = shot.get("scene", "generic")
    weather = shot.get("weather", "day")
    if scene in ("station", "seaside", "highway"):
        for index in range(8):
            x = (index * 71 + local_frame // 3) % 510 - 15
            y = 128 + (index % 4) * 13
            if scene != "station" or x < 252:
                _rect(draw, (x, y, x + 9 + index % 3 * 3, y + 1), colors["sea_light"])
    if scene in ("station", "seaside", "highway", "campus", "rain_street", "ancient"):
        if weather in ("day", "sunset"):
            x = (35 + local_frame // 2) % 540 - 30
            _line(draw, ((x, 92), (x + 4, 89), (x + 8, 92)), colors["sky_shadow"])
            _line(draw, ((x + 15, 101), (x + 18, 99), (x + 21, 101)), colors["sky_shadow"])
        elif weather == "night":
            for index in range(7):
                if (index + local_frame // 12) % 3:
                    _rect(draw, (42 + index * 61, 20 + index % 3 * 17, 43 + index * 61, 21 + index % 3 * 17), colors["cream"])
    else:
        for index in range(10):
            x = 20 + (index * 83 + local_frame // 4) % 440
            y = 37 + (index * 37) % 120
            if (index + local_frame // 8) % 3 == 0:
                _rect(draw, (x, y, x + 1, y + 1), colors["cream"])


def _weather(
    image: Image.Image,
    shot: dict,
    colors: dict,
    local_frame: int,
    context: dict,
    clip: tuple[int, int] | None = None,
) -> None:
    weather = shot.get("weather", "day")
    draw = ImageDraw.Draw(image)
    x_start, x_end = clip or (0, 480)
    width = max(1, x_end - x_start)
    if weather == "rain":
        rain = context.get("rain", [])
        for index, values in enumerate(rain[:64]):
            base_x, base_y, speed = (int(values[0]), int(values[1]), int(values[2]))
            x = x_start + (base_x + local_frame * 2 + index) % width
            y = (base_y + local_frame * speed) % 250
            _line(draw, ((x, y), (x - 3, y + 10)), colors["glass_light"])
    elif weather == "snow":
        for index in range(52):
            x = x_start + (index * 83 + local_frame * (1 + index % 2)) % width
            y = (index * 47 + local_frame * (2 + index % 3)) % 250
            size = 2 if index % 3 else 3
            _rect(draw, (x, y, x + size, y + size), colors["warm_white"])
    if shot.get("story_phase") == "memory":
        for index in range(18):
            x = x_start + (index * 97 + 31) % max(1, width - 10)
            y = (index * 41 + 23) % 180
            if (index + local_frame // 8) % 4 == 0:
                _rect(draw, (x, y, x + 2, y + 2), colors["cream"])


def _pixel_heart(draw, x: int, y: int, color, scale: int = 1) -> None:
    s = max(1, scale)
    _rect(draw, (x, y, x + 3 * s, y + 3 * s), color)
    _rect(draw, (x + 6 * s, y, x + 9 * s, y + 3 * s), color)
    _rect(draw, (x - 2 * s, y + 2 * s, x + 11 * s, y + 6 * s), color)
    _rect(draw, (x, y + 7 * s, x + 9 * s, y + 9 * s), color)
    _rect(draw, (x + 3 * s, y + 10 * s, x + 6 * s, y + 12 * s), color)


def _narrative_effects(image: Image.Image, shot: dict, colors: dict, local_frame: int, font_path: str) -> None:
    effects = set(shot.get("effects", []))
    if not effects:
        return
    draw = ImageDraw.Draw(image)
    if "stage_lights" in effects:
        for index, x in enumerate((72, 240, 408)):
            color = colors["pink"] if index % 2 else colors["amber"]
            draw.polygon(((x - 5, 0), (x + 5, 0), (x + 55, 190), (x - 55, 190)), fill=_mix(colors["violet"], color, 0.28))
            _rect(draw, (x - 8, 2, x + 8, 7), color)
    if "fireworks" in effects:
        for burst, (cx, cy, color) in enumerate(((112, 64, colors["coral"]), (345, 52, colors["amber"]), (252, 91, colors["pink"]))):
            phase = (local_frame + burst * 9) % 36
            radius = 5 + min(24, phase)
            if phase > 27:
                continue
            for ray in range(18):
                angle = math.tau * ray / 18
                inner = max(2, radius - 7)
                x0 = cx + int(math.cos(angle) * inner)
                y0 = cy + int(math.sin(angle) * inner)
                x1 = cx + int(math.cos(angle) * radius)
                y1 = cy + int(math.sin(angle) * radius)
                _line(draw, ((x0, y0), (x1, y1)), color)
                if phase > 16 and ray % 2 == 0:
                    _rect(draw, (x1, y1 + phase - 16, x1 + 1, y1 + phase - 14), color)
    if "pixel_hearts" in effects:
        for index in range(6):
            x = 58 + (index * 79 + int(shot.get("seed", 0))) % 390
            y = 190 - ((local_frame + index * 13) % 120)
            if index % 2 or local_frame % 4 < 2:
                _pixel_heart(draw, x, y, colors["pink"] if index % 2 else colors["coral"], 1)
    if "thought_bubble" in effects:
        _rect(draw, (312, 24, 449, 94), colors["warm_white"], colors["ink"], 2)
        _rect(draw, (321, 33, 440, 85), colors["sky_light"])
        _rect(draw, (321, 68, 440, 85), colors["sea"])
        _rect(draw, (340, 46, 365, 68), colors["train_cream"], colors["wood_dark"])
        _pixel_heart(draw, 395, 46, colors["coral"], 1)
        _rect(draw, (300, 100, 308, 108), colors["warm_white"], colors["ink"])
        _rect(draw, (290, 114, 295, 119), colors["warm_white"], colors["ink"])
    if "title_card" in effects:
        title = str(shot.get("title", "像素 · 纪行"))
        font = _font(font_path, 16)
        bounds = draw.textbbox((0, 0), title, font=font)
        width = bounds[2] - bounds[0] + 30
        _rect(draw, (20, 20, 20 + width, 58), colors["train_cream"], colors["ink"], 2)
        _rect(draw, (27, 28, 31, 50), colors["coral"])
        draw.text((40, 28 - bounds[1]), title, font=font, fill=colors["ink"])
    if "photo_frame" in effects:
        _rect(draw, (0, 0, 479, 8), colors["warm_white"])
        _rect(draw, (0, 247, 479, 269), colors["warm_white"])
        _rect(draw, (0, 0, 8, 269), colors["warm_white"])
        _rect(draw, (471, 0, 479, 269), colors["warm_white"])
        _rect(draw, (431, 252, 449, 260), colors["coral"])
    if "film_frame" in effects:
        _rect(draw, (0, 0, 479, 13), colors["black"])
        _rect(draw, (0, 256, 479, 269), colors["black"])
        for x in range(8, 480, 24):
            _rect(draw, (x, 3, x + 10, 10), colors["train_cream"])
            _rect(draw, (x, 259, x + 10, 266), colors["train_cream"])


def _static_art(shot: dict, colors: dict, font_path: str) -> Image.Image:
    scene = shot.get("scene", "generic")
    builder = _BACKGROUND.get(scene, _generic_background)
    compare = shot.get("compare_weather")
    if scene != "station" or not compare:
        return builder(shot, colors, font_path)

    image = Image.new("RGB", (480, 270), colors["ink"])
    for destination, crop_x, weather in ((0, 0, shot.get("weather", "day")), (240, 240, compare)):
        part = dict(shot)
        part["weather"] = weather
        part["compare_weather"] = None
        part["split_part"] = True
        if part.get("action") == "train":
            part["action"] = "waiting"
        layer = builder(part, colors, font_path)
        _station_foreground(layer, part, colors)
        image.paste(layer.crop((crop_x, 0, crop_x + 240, 270)), (destination, 0))
    return image


def render_art_frame(shot: dict, local_frame: int, style: dict, context: dict) -> Image.Image:
    colors = _palette(style)
    font_path = _font_path(style, context)
    key = (
        shot.get("id"),
        shot.get("scene"),
        shot.get("shot_type"),
        shot.get("weather"),
        shot.get("compare_weather"),
        shot.get("story_phase"),
        shot.get("action"),
    )
    if context.get("art_key") != key:
        context["art_static"] = _static_art(shot, colors, font_path)
        context["art_key"] = key
    image = context["art_static"].copy()
    _ambient(image, shot, colors, local_frame)
    compare = shot.get("compare_weather")
    if shot.get("action") == "train" and shot.get("scene") == "station" and not compare:
        _train(ImageDraw.Draw(image), colors, 300 - int(local_frame * 2))
    if shot.get("scene") == "station" and not compare:
        _station_foreground(image, shot, colors)
    _actors_and_props(image, shot, colors, local_frame)
    if compare:
        _weather(image, shot, colors, local_frame, context, (0, 240))
        other = dict(shot)
        other["weather"] = compare
        _weather(image, other, colors, local_frame, context, (240, 480))
    else:
        _weather(image, shot, colors, local_frame, context)
    _narrative_effects(image, shot, colors, local_frame, font_path)
    draw = ImageDraw.Draw(image)
    if compare:
        _rect(draw, (239, 0, 240, 269), colors["ink"])
    _rect(draw, (0, 0, 479, 3), colors["warm_white"])
    _rect(draw, (0, 266, 479, 269), colors["warm_white"])
    _rect(draw, (0, 0, 3, 269), colors["warm_white"])
    _rect(draw, (476, 0, 479, 269), colors["warm_white"])
    return image
