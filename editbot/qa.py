"""Проверка готового ролика: совпадают ли субтитры с голосом.

Готовый ролик расшифровывается заново (Whisper на видеокарте — секунды), и для каждого слова субтитров
ищется то же слово в речи рядом по времени. Расхождение = когда слово появилось на экране минус когда
прозвучало. Итог пишется в журнал; большое расхождение — предупреждение.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

log = logging.getLogger("qa")


def _norm(w: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", w.lower().replace("ё", "е"))


def _heard_at(heard: list[tuple[float, str]], t: float, w: str) -> float | None:
    """Когда слово w реально прозвучало рядом с моментом t (или None)."""
    cand = [ht for ht, hw in heard if abs(ht - t) < 2.5 and (hw == w or (len(w) > 4 and hw[:5] == w[:5]))]
    return min(cand, key=lambda x: abs(x - t)) if cand else None


def realign(subs: list[dict], heard: list[tuple[float, str]], tol: float = 0.3, limit: float = 1.6) -> int:
    """Переставляет слова субтитров на время, когда они прозвучали в готовом ролике (сдвиг tol…limit с).
    Порядок слов сохраняется; первое слово события двигает начало события. Возвращает число исправленных слов."""
    fixed, prev_end = 0, 0.0
    for ev in subs:
        words = ev["top"] + ev["main"]
        if not words:
            continue
        abs_t = [ev["start"] + w["t"] for w in words]
        new_t = list(abs_t)
        for i, (t, w) in enumerate(zip(abs_t, words)):
            n = _norm(w["text"])
            ht = _heard_at(heard, t, n) if len(n) >= 3 else None
            if ht is not None and tol < abs(ht - t) <= limit:
                new_t[i] = ht
        # порядок слов: каждое не раньше предыдущего, не позже следующего исправленного
        for i in range(1, len(new_t)):
            new_t[i] = max(new_t[i], new_t[i - 1] + 0.06)
        new_t[0] = max(new_t[0], prev_end - 0.15)
        if new_t != abs_t and new_t[-1] < ev["end"] - 0.2:
            fixed += sum(abs(a - b) > 1e-3 for a, b in zip(abs_t, new_t))
            ev["start"] = round(new_t[0], 3)
            for w, t in zip(words, new_t):
                w["t"] = round(max(0.0, t - ev["start"]), 3)
        prev_end = ev["start"] + words[-1]["t"]
    return fixed


def sync_report(cfg, video: Path, subs: list[dict]) -> dict | None:
    """subs — события субтитров (как в описании монтажа для After Effects): start + слова с t от начала."""
    from . import subtitles

    shown = [(ev["start"] + w["t"], _norm(w["text"])) for ev in subs for w in ev["top"] + ev["main"]]
    shown = [(t, w) for t, w in shown if len(w) >= 3]
    if len(shown) < 4:
        return None
    model = str(cfg.subtitles.get("film_model", "large-v3"))
    segs, _ = subtitles._model(model).transcribe(str(video), language=cfg.subtitles.language,
                                                 word_timestamps=True, vad_filter=True,
                                                 condition_on_previous_text=False)
    heard = [(w.start, _norm(w.word)) for s in segs for w in (s.words or []) if _norm(w.word)]
    offsets, missing = [], 0
    for t, w in shown:
        ht = _heard_at(heard, t, w)
        if ht is not None:
            offsets.append(ht - t)
        else:
            missing += 1
    if not offsets:
        return None
    off = np.array(offsets)
    rep = {"words": len(shown), "matched": len(offsets), "median_ms": int(np.median(off) * 1000),
           "late_or_early_over_300ms": int((np.abs(off) > 0.3).sum()), "max_ms": int(np.abs(off).max() * 1000),
           "heard": heard}
    msg = (f"Синхронизация субтитров «{video.name}»: сверено {rep['matched']} слов из {rep['words']}, "
           f"медиана {rep['median_ms']:+d} мс, расходятся > 0.3 с: {rep['late_or_early_over_300ms']}")
    if rep["matched"] < 0.6 * rep["words"]:
        # больше 40% слов на экране не звучат: субтитры выдуманы (чужой язык, музыка) — ролик не выкладывать
        rep["bad_text"] = True
        log.warning(msg + " — субтитры НЕ совпадают с речью, проверьте ролик")
    elif abs(rep["median_ms"]) > 200 or rep["late_or_early_over_300ms"] > 0.2 * rep["matched"]:
        log.warning(msg + " — заметное расхождение")
    else:
        log.info(msg)
    return rep


def jitter_report(video: Path, until: float) -> int:
    """Дрожь картинки до финала (там тряска задумана): кадр за кадром сдвиг меняет направление туда-обратно.
    Возвращает число таких «туда-обратно»; заметное количество — предупреждение в журнал."""
    import cv2

    from . import ffmpeg

    p = ffmpeg.popen_raw(["-t", f"{until:.2f}", "-i", str(video), "-vf", "scale=180:200", "-pix_fmt", "gray",
                          "-f", "rawvideo", "-"])
    fr = np.frombuffer(p.stdout.read(), np.uint8)
    p.wait()
    fr = fr[: len(fr) // 36000 * 36000].reshape(-1, 200, 180).astype(np.float32)
    if len(fr) < 10:
        return 0
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    # дрожь камеры — это сдвиг всего кадра: меряем отдельно верхнюю и нижнюю части (без полосы субтитров
    # посередине и плашки сверху) и считаем только когда обе части дёргаются одинаково — движение актёров
    # и проявление текста так не выглядят
    top = fr[:, 25:85, :]
    bot = fr[:, 150:200, :]
    sh_t = np.array([cv2.phaseCorrelate(top[i], top[i + 1])[0] for i in range(len(fr) - 1)])
    sh_b = np.array([cv2.phaseCorrelate(bot[i], bot[i + 1])[0] for i in range(len(fr) - 1)])
    flips = 0
    for i in range(2, len(sh_t)):
        if d[i] > 20 or d[i - 1] > 20 or d[i - 2] > 20:           # склейка
            continue
        for k in (0, 1):
            at, bt = sh_t[i - 1, k], sh_t[i, k]
            ab, bb = sh_b[i - 1, k], sh_b[i, k]
            same = abs(at - ab) < 0.5 and abs(bt - bb) < 0.5         # верх и низ сдвинулись одинаково
            if same and abs(at) > 0.4 and abs(bt) > 0.4 and np.sign(at) != np.sign(bt):
                flips += 1
    msg = f"Плавность «{video.name}»: дрожь до финала — {flips} смен направления"
    (log.warning if flips > 6 else log.info)(msg + (" — заметно дёргается" if flips > 6 else ""))
    return flips
