"""Промежуточные кадры для замедления по оптическому потоку (вместо простого смешивания двух кадров).

Смешивание даёт «двоение» на движении. Здесь для каждого пикселя ищется, куда он сдвинулся между кадрами
(DIS optical flow, OpenCV), и промежуточный кадр собирается из обоих соседних, сдвинутых на нужную долю
пути. Там, где поток ненадёжен (кадры не сходятся: смена плана, резкая вспышка), остаётся смешивание.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=1)
def _dis():
    import cv2

    return cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)


def between(f0: np.ndarray, f1: np.ndarray, t: float) -> np.ndarray:
    """Кадр на доле t (0..1) пути от f0 к f1 (RGB uint8)."""
    import cv2

    g0 = cv2.cvtColor(f0, cv2.COLOR_RGB2GRAY)
    g1 = cv2.cvtColor(f1, cv2.COLOR_RGB2GRAY)
    if np.abs(g0.astype(np.int16) - g1.astype(np.int16)).mean() > 40:     # разные планы — просто смесь
        return cv2.addWeighted(f0, 1 - t, f1, t, 0)
    dis = _dis()
    f01 = dis.calc(g0, g1, None)
    f10 = dis.calc(g1, g0, None)
    h, w = g0.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    # обратное отображение: пиксель промежуточного кадра берём из f0 (назад на t пути) и из f1 (вперёд)
    m0x, m0y = gx - t * f01[..., 0], gy - t * f01[..., 1]
    m1x, m1y = gx - (1 - t) * f10[..., 0], gy - (1 - t) * f10[..., 1]
    w0 = cv2.remap(f0, m0x, m0y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    w1 = cv2.remap(f1, m1x, m1y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    # где два сдвинутых кадра расходятся (перекрытия, края объектов) — берём ближний по времени кадр
    diff = np.abs(w0.astype(np.int16) - w1.astype(np.int16)).mean(axis=2)
    trust = np.clip(1.0 - (diff - 18) / 40, 0, 1)[..., None]
    out = (1 - t) * w0.astype(np.float32) + t * w1.astype(np.float32)
    near = w0 if t < 0.5 else w1
    out = trust * out + (1 - trust) * near.astype(np.float32)
    return out.round().astype(np.uint8)


def fix_duplicates(frames: np.ndarray, thr: float = 0.5) -> tuple[np.ndarray, int]:
    """Рипы с DVD часто содержат задвоенные кадры (A B B D вместо A B C D) — движение спотыкается.
    Повтор посреди движения заменяем кадром, дорисованным по движению между соседними (B ½(B→D) D).
    Возвращает (кадры, сколько заменено)."""
    import cv2

    n = len(frames)
    if n < 4:
        return frames, 0
    small = np.stack([cv2.resize(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY), (160, 90)) for f in frames]).astype(np.int16)
    d = np.abs(np.diff(small, axis=0)).mean(axis=(1, 2))          # d[i] — разница кадров i и i+1
    out, fixed = frames, 0
    for i in range(1, n - 1):
        # кадр i повторяет i-1, а вокруг движение (до и после разница заметная) — это задвоение
        if i < 2 or i >= len(d):
            continue
        around = min(d[i - 2], d[i])
        if around > 0.8 and d[i - 1] < 0.2 * around:
            if out is frames:
                out = frames.copy()
            out[i] = between(frames[i - 1], frames[i + 1], 0.5)
            fixed += 1
    return out, fixed
