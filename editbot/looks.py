"""Образы эдита под жанр фильма: цветокоррекция, палитра и анимация субтитров, переходы финальной нарезки.

Образ выбирается автоматически по жанрам и ключевым словам из справки исследователя (Википедия/TMDB),
его можно задать вручную: edit.look в config.yaml (auto / energy / pop / soft / dark).
"""
from __future__ import annotations

from dataclasses import dataclass, field


def ass_color(hex_rgb: str) -> str:
    """#RRGGBB -> &H00BBGGRR (формат ASS)."""
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


@dataclass
class Look:
    name: str
    grade: str                         # фильтры ffmpeg для картинки (до наложения полей)
    palette: list[str]                 # цвета слов субтитров (#RRGGBB), по кругу
    transitions: list[str]             # переходы на склейках финала: whip / zoomblur / rgb / flash
    shake: float = 14.0                # сила тряски на сильных долях (пиксели)
    music: list[str] = field(default_factory=list)   # запросы музыки, если исследователь не дал свои
    cine: str = ""                     # цвет для формата «arc» (как в эталонных эдитах): тёмный контрастный


# свечение только по яркости и в yuv444p (иначе цветовые каналы смешиваются и у края появляется полоса)
_GLOW = ("format=yuv444p,split[g0][g1];[g1]gblur=sigma={s}[g2];"
         "[g0][g2]blend=c0_mode=screen:c0_opacity={o}:c1_opacity=0:c2_opacity=0,format=yuv420p")

LOOKS = {
    # экшен, гонки, триллер: холодные тени/тёплые света, жёсткий контраст, неон, RGB-сдвиг и рывки
    "energy": Look(
        "energy",
        "eq=contrast=1.14:saturation=1.30:gamma=0.96,"
        "colorbalance=rs=-0.05:bs=0.06:rh=0.06:bh=-0.05," + _GLOW.format(s=22, o=0.18) +
        ",unsharp=5:5:0.8:3:3:0.4",
        ["#35F2FF", "#FFE14D", "#FF4FD8", "#7CFF5B", "#FF9A3D"],
        ["whip", "rgb", "zoomblur", "rgb"], 16, ["phonk", "hip hop beat", "electronic"]),
    # комедия, ситком, мультфильм: сочный тёплый цвет, радужные субтитры, весёлые «наезды»
    "pop": Look(
        "pop",
        "eq=contrast=1.12:saturation=1.40:gamma=0.97,colorbalance=rh=0.04:gh=0.02:bh=-0.03,"
        + _GLOW.format(s=18, o=0.14) + ",unsharp=5:5:0.9:3:3:0.4",
        ["#3DE8FF", "#FFE94A", "#FF5CC8", "#8CFF5A", "#B78CFF", "#FF9D3D"],
        ["zoomblur", "whip", "flash"], 12, ["funny", "upbeat", "hip hop beat"]),
    # драма, мелодрама: мягкое свечение, тёплые полутона, спокойные переходы, без тряски
    "soft": Look(
        "soft",
        "eq=contrast=1.06:saturation=1.12:gamma=1.0,colorbalance=rm=0.03:bm=-0.02,"
        + _GLOW.format(s=30, o=0.24) + ",unsharp=5:5:0.4",
        ["#FFFFFF", "#FFD98A", "#FFB3C7", "#BFE6FF"],
        ["zoomblur", "flash"], 0, ["emotional piano", "sad", "ambient"]),
    # ужасы, мистика: приглушённый цвет, глубокие тени, красный акцент, глитч
    "dark": Look(
        "dark",
        "eq=contrast=1.18:saturation=0.82:gamma=0.92,colorbalance=rs=-0.03:gs=-0.02:bs=0.05,"
        + _GLOW.format(s=24, o=0.12) + ",unsharp=5:5:0.6",
        ["#FFFFFF", "#FF3B3B", "#E0E0E0"],
        ["rgb", "whip"], 10, ["dark ambient", "horror", "suspense"]),
}

# «Кино»-цвет эталонных эдитов: глубокий чёрный, бирюзовые тени, тёплая кожа, сильная резкость,
# свечение светлых мест и виньетка. Отличается по жанрам только оттенком и силой.
_CINE = ("eq=contrast={c}:saturation={sat}:gamma={g}:brightness={br},"
         "curves=master='0/0 0.10/0.05 0.5/0.5 0.88/0.95 1/1',"
         "colorbalance=rs={rs}:gs=-0.01:bs={bs}:rm=-0.04:gm=0.0:bm=0.04:rh={rh}:gh=0.01:bh={bh},"
         + _GLOW.format(s=26, o="{glow}") + ",unsharp=7:7:{sharp}:5:5:0.0,vignette=PI/4.6")
_CINE_PARAMS = {
    "energy": dict(c=1.16, sat=1.06, g=0.95, br=-0.015, rs=-0.11, bs=0.14, rh=0.05, bh=-0.06, glow=0.20, sharp=1.1),
    "pop": dict(c=1.12, sat=1.18, g=0.97, br=0.0, rs=-0.05, bs=0.07, rh=0.05, bh=-0.04, glow=0.16, sharp=1.0),
    "soft": dict(c=1.08, sat=1.10, g=1.0, br=0.0, rs=-0.03, bs=0.06, rh=0.05, bh=-0.03, glow=0.26, sharp=0.8),
    "dark": dict(c=1.20, sat=0.90, g=0.93, br=-0.02, rs=-0.06, bs=0.10, rh=0.03, bh=-0.04, glow=0.14, sharp=1.0),
}
for _name, _look in LOOKS.items():
    _look.cine = _CINE.format(**_CINE_PARAMS[_name])

_RULES = [
    (("ужас", "horror", "мистик", "триллер", "thriller"), "dark"),
    (("комеди", "comedy", "ситком", "sitcom", "мультфильм", "анимац", "animation", "сказк"), "pop"),
    (("гонк", "racing", "drift", "дрифт", "боевик", "action", "фантаст", "science fiction", "героик",
      "superhero"), "energy"),
    (("драм", "drama", "мелодрам", "romance"), "soft"),
]


def choose(setting: str, genres: list[str], keywords: list[str]) -> Look:
    if setting in LOOKS:
        return LOOKS[setting]
    hay = " ".join([*genres, *keywords]).lower()
    for keys, name in _RULES:
        if any(k in hay for k in keys):
            return LOOKS[name]
    return LOOKS["pop"]
