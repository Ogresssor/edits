"""Оформление канала «КиноМомент» в стиле наших эдитов: тёмный фон, неоновое свечение, Montserrat.

    python tools/make_branding.py

Пишет в branding/: avatar.png (800×800), banner.png (2560×1440, всё важное — в безопасной зоне 1546×423
по центру, её видно на телефоне), watermark.png (150×150, значок поверх видео), banner_preview.png
(как шапка выглядит на телефоне).
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "models" / "fonts"
OUT = ROOT / "branding"
CYAN = (53, 242, 255)
MAGENTA = (255, 79, 216)
NAME = ("Кино", "Момент")


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def background(w: int, h: int) -> Image.Image:
    """Тёмный фон с мягкими цветными пятнами света (бирюза слева, пурпур справа) и лёгким зерном."""
    img = Image.new("RGB", (w, h), (7, 8, 14))
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(glow)
    r = int(max(w, h) * 0.35)
    d.ellipse([int(w * 0.18) - r, int(h * 0.55) - r, int(w * 0.18) + r, int(h * 0.55) + r], fill=(0, 70, 90))
    d.ellipse([int(w * 0.82) - r, int(h * 0.45) - r, int(w * 0.82) + r, int(h * 0.45) + r], fill=(80, 0, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(r * 0.6))
    img = Image.blend(img, glow, 0.85)
    # тонкие горизонтальные «киноленточные» полосы — едва заметная фактура
    lines = Image.new("L", (w, h), 0)
    ld = ImageDraw.Draw(lines)
    for y in range(0, h, 6):
        ld.line([(0, y), (w, y)], fill=10)
    img = Image.composite(Image.new("RGB", (w, h), (30, 30, 40)), img, lines)
    return img


def neon_text(img: Image.Image, xy: tuple[int, int], text: str, fnt: ImageFont.FreeTypeFont, color, anchor="mm",
              glow_px: int = 28) -> None:
    """Текст с неоновым свечением: несколько размытых слоёв цвета + светлое ядро."""
    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text(xy, text, font=fnt, fill=color + (255,), anchor=anchor)
    for radius, alpha in ((glow_px * 2.2, 0.55), (glow_px, 0.8), (glow_px * 0.35, 0.9)):
        blur = layer.filter(ImageFilter.GaussianBlur(radius))
        a = blur.getchannel("A").point(lambda v, k=alpha: int(min(255, v * k * 1.6)))
        blur.putalpha(a)
        img.alpha_composite(blur) if img.mode == "RGBA" else img.paste(blur, (0, 0), blur)
    core = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(core).text(xy, text, font=fnt, fill=tuple(min(255, c + 150) for c in color) + (255,), anchor=anchor)
    img.paste(core, (0, 0), core)


def play_frame(d: ImageDraw.ImageDraw, cx: int, cy: int, size: int, width: int, color) -> None:
    """Значок: рамка кадра со скруглёнными углами и треугольник «плей»."""
    w2, h2 = size, int(size * 0.72)
    d.rounded_rectangle([cx - w2 // 2, cy - h2 // 2, cx + w2 // 2, cy + h2 // 2], radius=int(size * 0.12),
                        outline=color, width=width)
    t = size * 0.2
    d.polygon([(cx - t * 0.7, cy - t), (cx - t * 0.7, cy + t), (cx + t, cy)], fill=color)


def glow_shape(img: Image.Image, draw_fn, color, glow_px: int) -> None:
    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer), color + (255,))
    for radius in (glow_px * 2, glow_px, glow_px * 0.3):
        blur = layer.filter(ImageFilter.GaussianBlur(radius))
        img.paste(blur, (0, 0), blur)
    core = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(core), tuple(min(255, c + 140) for c in color) + (255,))
    img.paste(core, (0, 0), core)


def avatar() -> Image.Image:
    s = 800
    img = background(s, s).convert("RGBA")
    # неоновое кольцо: бирюза -> пурпур
    ring = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    for i in range(360):
        a0 = math.radians(i - 90)
        k = (math.sin(math.radians(i)) + 1) / 2
        col = tuple(int(CYAN[j] * (1 - k) + MAGENTA[j] * k) for j in range(3)) + (255,)
        r0 = 330
        x0, y0 = s / 2 + r0 * math.cos(a0), s / 2 + r0 * math.sin(a0)
        rd.ellipse([x0 - 9, y0 - 9, x0 + 9, y0 + 9], fill=col)
    for radius in (40, 18, 5):
        blur = ring.filter(ImageFilter.GaussianBlur(radius))
        img.paste(blur, (0, 0), blur)
    img.paste(ring, (0, 0), ring)
    glow_shape(img, lambda d, c: play_frame(d, s // 2, s // 2 - 40, 300, 22, c), CYAN, 22)
    neon_text(img, (s // 2, s // 2 + 190), "КИНО МОМЕНТ", font("Montserrat-ExtraBold.ttf", 62), MAGENTA, glow_px=16)
    return img.convert("RGB")


def banner() -> Image.Image:
    w, h = 2560, 1440
    img = background(w, h).convert("RGBA")
    cy = h // 2
    # безопасная зона 1546×423 — всё главное в ней
    neon_text(img, (w // 2 - 185, cy - 25), NAME[0], font("Montserrat-ExtraBoldItalic.ttf", 135), CYAN, anchor="rm",
              glow_px=30)
    neon_text(img, (w // 2 - 25, cy - 25), NAME[1], font("Montserrat-ExtraBoldItalic.ttf", 135), MAGENTA,
              anchor="lm", glow_px=30)
    glow_shape(img, lambda d, c: play_frame(d, w // 2 - 105, cy - 18, 96, 10, c), (255, 255, 255), 10)
    neon_text(img, (w // 2, cy + 95), "ЛУЧШИЕ СЦЕНЫ ФИЛЬМОВ И СЕРИАЛОВ  •  ДВА ЭДИТА КАЖДЫЙ ДЕНЬ",
              font("Montserrat-SemiBold.ttf", 33), (235, 235, 245), glow_px=8)
    # тонкие неоновые линии по краям безопасной зоны
    def lines(d, c):
        d.line([(w // 2 - 640, cy + 150), (w // 2 - 90, cy + 150)], fill=c, width=3)
        d.line([(w // 2 + 90, cy + 150), (w // 2 + 640, cy + 150)], fill=c, width=3)
    glow_shape(img, lines, CYAN, 6)
    return img.convert("RGB")


def watermark() -> Image.Image:
    s = 150
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow_shape(img, lambda d, c: play_frame(d, s // 2, s // 2, 96, 9, c), CYAN, 6)
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    avatar().save(OUT / "avatar.png")
    b = banner()
    b.save(OUT / "banner.png")
    # как шапку видно на телефоне: только безопасная зона 1546×423
    w, h = b.size
    b.crop(((w - 1546) // 2, (h - 423) // 2, (w + 1546) // 2, (h + 423) // 2)).save(OUT / "banner_preview_phone.png")
    watermark().save(OUT / "watermark.png")
    print("готово:", ", ".join(p.name for p in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
