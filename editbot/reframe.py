"""Кадрирование под вертикальный экран: окно 9:16 стоит на главном герое, как камера оператора.

Всё считается по КАДРАМ ИСХОДНИКА (а не по времени результата), поэтому перестановка окна происходит ровно
на кадре склейки — без кадра «чужого» кадрирования до или после неё (раньше из-за пересчёта 24 -> 30 к/с
окно иногда прыгало на кадр раньше/позже склейки — это и было видно как рывок).

  1. на каждом кадре ищем склейки (гистограмма яркости), лица — нейросетью YuNet ~6 раз в секунду;
  2. внутри плана камера стоит неподвижно на медиане положения героя;
     если герой уходит далеко — одна плавная панорама с разгоном и торможением;
  3. положение окна дробное: при панораме кадр сдвигается на доли пикселя исходника (после увеличения —
     на 1 пиксель результата), без ступенек.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np

log = logging.getLogger("reframe")

ANALYSIS_FPS = 6
ANALYSIS_W = 480
CUT_THRESHOLD = 0.45
MIN_SHOT_SEC = 0.35       # «план» короче — скорее вспышка/резкое движение, окно не переставляем


YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


@lru_cache(maxsize=1)
def _model_path() -> str:
    import requests

    from .config import ROOT

    path = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Скачиваю модель поиска лиц YuNet (~230 КБ)…")
        r = requests.get(YUNET_URL, timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
    return str(path)


def _detector(w: int, h: int):
    import cv2

    try:  # без предупреждений dnn в консоли (API отличается между версиями OpenCV)
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    # модель читаем сами и передаём буфером: OpenCV не открывает файлы по путям с кириллицей
    model = np.fromfile(_model_path(), np.uint8)
    return cv2.FaceDetectorYN.create("onnx", model, np.array([], np.uint8), (w, h), 0.6, 0.3, 50)


def analyze_frames(frames: np.ndarray, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """frames: (N, H, W, 3) RGB. Возвращает центр интереса по ширине (0..1, NaN — нет данных)
    и признак склейки для каждого кадра."""
    import cv2

    n, src_h, src_w = frames.shape[:3]
    h = int(ANALYSIS_W * src_h / src_w) // 2 * 2
    detector = _detector(ANALYSIS_W, h)
    every = max(1, int(round(fps / ANALYSIS_FPS)))
    centers = np.full(n, np.nan, np.float32)
    cuts = np.zeros(n, bool)
    prev = prev_hist = None
    last_face: float | None = None
    since_cut = 0
    last_cut = -10 ** 9
    min_shot = max(1, int(MIN_SHOT_SEC * fps))
    for i in range(n):
        small = cv2.resize(frames[i], (ANALYSIS_W, h), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        hist = np.histogram(g, bins=32, range=(0, 255))[0] / g.size
        cut = prev_hist is not None and np.abs(hist - prev_hist).sum() > CUT_THRESHOLD
        if cut and i - last_cut < min_shot:
            cut = False  # две «склейки» подряд — это вспышка/движение, а не новый план
        if cut:
            last_cut = i
        cuts[i] = cut
        if cut:
            last_face = None
        since_cut = 0 if cut else since_cut + 1
        # лица ищем раз в `every` кадров и обязательно на первом кадре после склейки
        if since_cut % every == 0:
            _, faces = detector.detect(cv2.cvtColor(small, cv2.COLOR_RGB2BGR))
            if faces is not None and len(faces):
                # главный герой кадра — самое крупное уверенное лицо; но если в плане мы уже ведём человека,
                # держимся за него (иначе окно мечется между двумя собеседниками одинакового размера)
                big = max(faces, key=lambda f: f[2] * f[3] * f[-1])
                f = big
                if not cut and last_face is not None:
                    near = [x for x in faces if x[2] * x[3] >= 0.5 * big[2] * big[3]]
                    f = min(near, key=lambda x: abs((x[0] + x[2] / 2) / ANALYSIS_W - last_face))
                centers[i] = (f[0] + f[2] / 2) / ANALYSIS_W
                last_face = float(centers[i])
            elif prev is not None and not cut:
                diff = cv2.GaussianBlur(cv2.absdiff(g, prev), (9, 9), 0).astype(np.float32)
                col = diff.sum(axis=0)
                if col.sum() > ANALYSIS_W * h * 2.5:  # заметное движение
                    centers[i] = float((col * np.arange(ANALYSIS_W)).sum() / col.sum()) / ANALYSIS_W
        prev, prev_hist = g, hist
    return centers, cuts


def _ease(u: np.ndarray) -> np.ndarray:
    """Плавный разгон и торможение панорамы (smoothstep)."""
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3 - 2 * u)


def plan_path(centers: np.ndarray, cuts: np.ndarray, fps: float, half: float, mode: str = "smart") -> np.ndarray:
    """Центр окна (0..1 по ширине) для каждого кадра исходника.
    half — половина ширины окна в долях ширины кадра."""
    n = len(centers)
    if mode == "center" or n == 0:
        return np.full(n, 0.5)
    if mode == "track":
        return _track_path(centers, cuts, fps, half)
    pan_allowed = mode == "smart"
    every = max(1.0, fps / ANALYSIS_FPS)
    path = np.full(n, 0.5)
    bounds = [0, *np.flatnonzero(cuts).tolist(), n]
    prev_pos = 0.5
    first = True
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        seg = centers[a:b]
        known = seg[~np.isnan(seg)]
        checked = max(1.0, (b - a) / every)       # сколько кадров плана проверяли на лица/движение
        if len(known) >= max(1.0, 0.25 * checked) or (len(known) and (b - a) > fps):
            pos = float(np.median(known))
        elif first:
            pos = 0.5
        else:
            pos = prev_pos if (b - a) <= fps else 0.5  # короткий «пустой» план — не дёргаем
        pos = float(np.clip(pos, half, 1 - half))
        if not first and abs(pos - prev_pos) < 0.04:  # почти то же место — не переставляем камеру
            pos = prev_pos
        p0 = p1 = pos
        if pan_allowed and len(known) >= 0.5 * checked and (b - a) >= 2 * fps:
            # панорама, только если герой уходит дальше ширины окна: от позиции в первой трети к последней
            third = max(1, (b - a) // 3)
            s_first = seg[:third][~np.isnan(seg[:third])]
            s_last = seg[-third:][~np.isnan(seg[-third:])]
            if len(s_first) and len(s_last):
                s0, s1 = float(np.median(s_first)), float(np.median(s_last))
                if abs(s1 - s0) > half * 1.6:
                    p0, p1 = float(np.clip(s0, half, 1 - half)), float(np.clip(s1, half, 1 - half))
        if p0 == p1:
            path[a:b] = p0
        else:
            path[a:b] = p0 + (p1 - p0) * _ease(np.arange(b - a) / max(1, b - a - 1))
        prev_pos, first = p1, False
    return path


def _track_path(centers: np.ndarray, cuts: np.ndarray, fps: float, half: float,
                sigma_sec: float = 0.45) -> np.ndarray:
    """Камера ведёт лицо и держит его по центру — плавно, как стабилизированный оператор.
    Внутри плана: известные положения интерполируются, затем сглаживаются гауссом (~0.45 с) — мелкая дрожь
    детектора исчезает, а реальное движение героя камера повторяет. На склейке — мгновенная перестановка."""
    n = len(centers)
    path = np.full(n, 0.5)
    bounds = [0, *np.flatnonzero(cuts).tolist(), n]
    prev = 0.5
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        seg = centers[a:b].astype(np.float64)
        idx = np.flatnonzero(~np.isnan(seg))
        if len(idx) == 0:
            path[a:b] = prev if (b - a) <= fps else 0.5
            continue
        x = np.interp(np.arange(b - a), idx, seg[idx])
        sig = sigma_sec * fps
        if b - a > 2 and sig > 0.5:
            k = np.arange(-int(3 * sig), int(3 * sig) + 1)
            w = np.exp(-0.5 * (k / sig) ** 2)
            w /= w.sum()
            pad = np.pad(x, (len(k) // 2, len(k) // 2), mode="edge")
            x = np.convolve(pad, w, mode="valid")
        # «ленивый оператор»: пока лицо в центральной зоне окна — камера стоит; вышло за зону — камера
        # сдвигается ровно настолько, чтобы вернуть его в зону, и снова стоит. Мелкие движения головы
        # не превращаются в «гуляющую» картинку
        dz = 0.45 * half
        cam = np.empty_like(x)
        c = float(np.median(x[: max(1, int(fps))]))
        for i, v in enumerate(x):
            if v > c + dz:
                c = v - dz
            elif v < c - dz:
                c = v + dz
            cam[i] = c
        if b - a > 2:                                   # сдвиги — плавные (разгон и торможение)
            sig2 = 0.35 * fps
            k = np.arange(-int(3 * sig2), int(3 * sig2) + 1)
            w = np.exp(-0.5 * (k / sig2) ** 2)
            w /= w.sum()
            cam = np.convolve(np.pad(cam, (len(k) // 2, len(k) // 2), mode="edge"), w, mode="valid")
        if np.ptp(cam) < 0.02:
            cam[:] = np.median(cam)
        path[a:b] = np.clip(cam, half, 1 - half)
        prev = float(path[b - 1])
    return path


class Framer:
    """Вырезает окно 9:16 из кадров (с дробным сдвигом) и увеличивает до out_w × out_h.
    upscaler — Real-ESRGAN x4 (editbot/upscale.py) или None (lanczos)."""

    def __init__(self, src_w: int, src_h: int, out_w: int, out_h: int, upscaler=None):
        self.src_w, self.src_h, self.out_w, self.out_h = src_w, src_h, out_w, out_h
        self.up = upscaler
        ar = out_w / out_h
        if src_w / src_h >= ar:     # обычный широкий кадр: окно во всю высоту
            self.k = out_h / src_h                     # во сколько раз увеличиваем
            self.win = src_h * ar                      # ширина окна в пикселях исходника (дробная)
        else:                       # кадр уже 9:16 — берём всю ширину по центру
            self.k = out_w / src_w
            self.win = src_w
        self.region = min(src_w, int(np.ceil(self.win)) + 2)   # с запасом под дробный сдвиг
        self.rows = (0, src_h) if src_w / src_h >= ar else (
            int((src_h - out_h / self.k) / 2), int((src_h + out_h / self.k) / 2))

    @property
    def half(self) -> float:
        return self.win / 2 / self.src_w

    def __call__(self, frames: np.ndarray, centers: np.ndarray) -> np.ndarray:
        """frames (N, H, W, 3) RGB + центр окна на каждый кадр -> (N, out_h, out_w, 3)."""
        import cv2

        n = len(frames)
        P = 6   # поля с отражением: нейросеть портит пару пикселей у края куска — край потом срезаем
        left = np.clip(centers * self.src_w - self.win / 2, 0, self.src_w - self.win)
        x0 = np.clip(np.floor(left).astype(int), 0, self.src_w - self.region)
        frac = left - x0                                   # дробная часть сдвига (0..2)
        r0, r1 = self.rows
        crops = []
        for i in range(n):
            f = cv2.copyMakeBorder(frames[i, r0:r1], P, P, P, P, cv2.BORDER_REFLECT_101)
            crops.append(f[:, x0[i]:x0[i] + self.region + 2 * P])
        crops = np.stack(crops)
        if self.up is not None:
            big = self.up(crops)                           # x4
            scale = self.k / 4
        else:
            big, scale = crops, self.k
        out = np.empty((n, self.out_h, self.out_w, 3), np.uint8)
        rw = int(round((self.region + 2 * P) * self.k))
        rh = int(round((r1 - r0 + 2 * P) * self.k))
        top = int(round(P * self.k))
        for i in range(n):
            img = cv2.resize(big[i], (rw, rh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4)
            off = int(round((frac[i] + P) * self.k))
            off = min(max(0, off), rw - self.out_w)
            out[i] = img[top:top + self.out_h, off:off + self.out_w]
        return out
