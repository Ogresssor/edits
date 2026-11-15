"""Вшитые в картинку субтитры фильма (например, перевод японских реплик в «Токийском дрифте»).

Если в куске фильма внизу кадра есть строка текста, наши субтитры и чужие наложатся друг на друга.
Признаки строки текста:
  * много мелких ярких белых/жёлтых «букв» с резкими краями в нижней трети кадра;
  * буквы одной высоты, выстроены по одной горизонтали, строка около центра по ширине;
  * строка неподвижна: через 0.15 с на том же месте те же пиксели (осколки, блики, огни — двигаются).
Найдено — кадр плавно увеличивается с привязкой к верху, чтобы нижняя полоса с текстом ушла за край.
"""
from __future__ import annotations

import numpy as np


def text_line(rgb: np.ndarray) -> tuple[float, tuple[int, int, int, int]] | None:
    """(верх строки текста как доля высоты, рамка строки x0, y0, x1, y1) или None."""
    import cv2

    h, w = rgb.shape[:2]
    y0 = int(h * 0.62)
    band_rgb = rgb[y0:].astype(np.int16)
    band = cv2.cvtColor(np.ascontiguousarray(rgb[y0:]), cv2.COLOR_RGB2GRAY)
    chroma = band_rgb.max(axis=2) - band_rgb.min(axis=2)
    yellow = (band_rgb[..., 0] > 170) & (band_rgb[..., 1] > 170) & (band_rgb[..., 2] < 120)
    bright = (band > 175) & ((chroma < 45) | yellow)
    edges = cv2.dilate(cv2.Canny(band, 80, 200), np.ones((3, 3), np.uint8)) > 0
    mask = (bright & edges).astype(np.uint8)
    n, _, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    hs = max(6, h // 70)
    ok = [(stats[i], cent[i]) for i in range(1, n)
          if hs <= stats[i][3] <= h // 12 and 1 <= stats[i][2] <= w // 14 and stats[i][4] >= 6
          and 0.10 * w < cent[i][0] < 0.82 * w]
    if len(ok) < 8:
        return None
    # строка может быть наклонной (титры в перспективе): прямая через центры букв, лишние точки отбрасываем
    xs_all = np.array([c[0] for _, c in ok])
    ys_all = np.array([c[1] for _, c in ok])
    tol = h * 0.02
    best = None
    for i in range(len(ok)):                       # RANSAC по парам точек: прямая, на которой больше всего букв
        for j in range(i + 1, len(ok)):
            dx = xs_all[j] - xs_all[i]
            if abs(dx) < w * 0.05:
                continue
            a = (ys_all[j] - ys_all[i]) / dx
            if abs(a) > 0.25:
                continue
            inl = np.abs(ys_all - (ys_all[i] + a * (xs_all - xs_all[i]))) < tol
            if best is None or inl.sum() > best.sum():
                best = inl
    if best is None or best.sum() < 8:
        return None
    a, b = np.polyfit(xs_all[best], ys_all[best], 1)
    keep = np.abs(ys_all - (a * xs_all + b)) < tol
    row = [ok[i] for i in np.nonzero(keep)[0]]
    if len(row) < 8:
        return None
    xs = np.array([c[0] for _, c in row])
    if xs.max() - xs.min() < w * 0.22 or abs(xs.mean() - w / 2) > 0.15 * w:   # строка по центру
        return None
    hts = np.array([s[3] for s, _ in row], float)
    if np.std(hts) > 0.45 * np.median(hts):       # буквы одной высоты
        return None
    bx0 = int(min(s[0] for s, _ in row))
    bx1 = int(max(s[0] + s[2] for s, _ in row))
    by0 = int(min(s[1] for s, _ in row)) + y0
    by1 = int(max(s[1] + s[3] for s, _ in row)) + y0
    return by0 / h, (bx0, by0, bx1, by1)


def detect(frames: np.ndarray, fps: float = 24.0) -> float | None:
    """Для куска фильма (RGB): доля высоты, где начинаются вшитые субтитры (если неподвижная строка
    текста есть хотя бы в пятой части проверенных кадров), иначе None."""
    import cv2

    gap = max(2, int(round(fps * 0.1)))
    step = max(1, len(frames) // 16)
    tops = []
    k = 0
    for i in range(0, max(1, len(frames) - gap), step):
        k += 1
        r = text_line(frames[i])
        if r is None:
            continue
        top, (x0, y0, x1, y1) = r
        # строка стоит на месте: светлые пиксели букв через 0.15 с там же (фон за текстом может двигаться)
        a = cv2.cvtColor(np.ascontiguousarray(frames[i, y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY) > 175
        b = cv2.cvtColor(np.ascontiguousarray(frames[i + gap, y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY) > 175
        b = cv2.dilate(b.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0   # титры могут медленно плыть
        if a.sum() > 20 and (a & b).sum() / a.sum() > 0.7:
            tops.append(top)
    if tops and len(tops) >= max(1, k // 5):
        return float(min(tops))
    return None
