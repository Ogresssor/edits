"""Поиск и удаление логотипа канала (вотермарки) в углу кадра — например, «STARZ» в рипах с ТВ.

Логотип стоит на одном месте весь фильм (или большую его часть), а картинка вокруг меняется. Поэтому:
  1. берём ~240 кадров равномерно по фильму и ищем контуры (Canny);
  2. пиксель, который оказывается контуром в заметной доле кадров, — часть логотипа
     (у обычной картинки контуры «гуляют» и в одном месте повторяются редко);
  3. ищем такие пиксели только у краёв кадра (логотипы не ставят в центр);
  4. маску расширяем и сохраняем в work/<фильм>/watermark.png.
При монтаже область маски закрашивается по соседним пикселям (cv2.inpaint) до увеличения кадра.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from . import ffmpeg

log = logging.getLogger("watermark")

VERSION = 2


def detect(movie: Path, info: ffmpeg.MediaInfo, workdir: Path, samples: int = 240) -> np.ndarray | None:
    import cv2

    cache = workdir / "watermark.npz"
    if cache.exists():
        d = np.load(cache)
        if "v" in d.files and int(d["v"]) == VERSION:
                return np.stack([d["mask"], d["core"]]) if d["found"] else None
    w, h = info.width, info.height
    step = max(1.0, info.duration * 0.9 / samples)
    proc = ffmpeg.popen_raw(["-ss", f"{info.duration * 0.05:.1f}", "-i", str(movie), "-map", "0:v:0", "-an",
                             "-vf", f"fps=1/{step:.3f}", "-frames:v", str(samples),
                             "-pix_fmt", "gray", "-f", "rawvideo", "-"])
    assert proc.stdout
    freq = np.zeros((h, w), np.float32)
    n = 0
    while True:
        buf = proc.stdout.read(w * h)
        if len(buf) < w * h:
            break
        g = np.frombuffer(buf, np.uint8).reshape(h, w)
        if g.mean() < 12:                       # чёрные кадры (затемнения) не считаем
            continue
        freq += cv2.Canny(g, 60, 160) > 0
        n += 1
    proc.wait()
    found = False
    mask = np.zeros((h, w), np.uint8)
    if n >= 30:
        freq /= n
        # только у краёв: верхняя/нижняя четверть или левая/правая пятая кадра (логотипы не ставят в центр)
        yy, xx = np.mgrid[0:h, 0:w]
        border = (yy < h // 4) | (yy >= h - h // 4) | (xx < w // 5) | (xx >= w - w // 5)
        hot = (freq > 0.12) & border   # логотип бывает не весь фильм — 12% кадров уже много
        # отдельные «горячие» пиксели (полосы рамки, края кадра) — шум; логотип — компактное пятно
        hot[:3, :] = hot[-3:, :] = False
        hot[:, :3] = hot[:, -3:] = False
        num, labels, stats, _ = cv2.connectedComponentsWithStats(
            cv2.dilate(hot.astype(np.uint8), np.ones((5, 5), np.uint8)), 8)
        for k in range(1, num):
            x, y, bw, bh, area = stats[k]
            if 20 <= bw <= w * 0.3 and 8 <= bh <= h * 0.25 and area >= 150:
                mask[labels == k] = 255
                found = True
        if found:
            mask = cv2.dilate(mask, np.ones((7, 7), np.uint8))
            ys, xs = np.nonzero(mask)
            log.info("Найдена вотермарка в кадре: x %d–%d, y %d–%d — будет закрашена", xs.min(), xs.max(),
                     ys.min(), ys.max())
        core = (hot & (mask > 0)).astype(np.uint8) * 255   # «скелет» логотипа — для проверки, виден ли он
    else:
        core = mask
    np.savez_compressed(cache, mask=mask, core=core if found else mask, found=found, v=VERSION)
    return np.stack([mask, core]) if found else None


def remove(frames: np.ndarray, mask_core: np.ndarray) -> np.ndarray:
    """Закрашивает область логотипа на кадрах (RGB) по соседним пикселям — если логотип в этом куске виден
    (контуры совпадают со «скелетом» логотипа хотя бы на трети кадров). Работает только по рамке маски."""
    import cv2

    mask, core = mask_core[0], mask_core[1]
    ys, xs = np.nonzero(mask)
    y0, y1 = max(0, ys.min() - 8), min(mask.shape[0], ys.max() + 9)
    x0, x1 = max(0, xs.min() - 8), min(mask.shape[1], xs.max() + 9)
    m = mask[y0:y1, x0:x1]
    c = core[y0:y1, x0:x1] > 0
    seen = 0
    for i in range(0, len(frames), max(1, len(frames) // 12)):
        g = cv2.cvtColor(np.ascontiguousarray(frames[i, y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY)
        e = cv2.dilate(cv2.Canny(g, 60, 160), np.ones((3, 3), np.uint8)) > 0
        seen += (e & c).sum() >= 0.35 * max(1, c.sum())
    if seen < max(1, min(12, len(frames)) // 3):
        return frames                                  # в этом куске логотипа нет
    out = frames.copy()
    for i in range(len(frames)):
        roi = np.ascontiguousarray(out[i, y0:y1, x0:x1])
        out[i, y0:y1, x0:x1] = cv2.inpaint(roi, m, 4, cv2.INPAINT_TELEA)
    return out
