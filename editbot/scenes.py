"""Автоподбор сцен для эдитов — работает без Claude.

Формат эдита (как в эталоне): ОДНА цельная сцена фильма с диалогом (родные склейки, субтитры) и в конце —
быстрая нарезка лучших кадров этой же сцены под бит. Поэтому в ролике нет «чужих» кадров из других мест.

Сцена-кандидат:
  * начинается и заканчивается на границе фразы (пауза в речи или конец предложения);
  * 14–26 секунд, внутри нет долгого молчания (> 4 с — скорее всего, смена сцены);
  * не из первых минут (логотипы), не из финала фильма (спойлеры).
Оценка: плотность речи, смена реплик (диалог живее монолога), восклицания/вопросы, громкость и движение,
совпадение с известными цитатами (агент-исследователь), сильная последняя фраза (панчлайн).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("scenes")

SIGNAL_FPS = 2.0
# частые «галлюцинации» Whisper на музыке/тишине и служебный текст
JUNK = re.compile(r"субтитр|редактор|продолжение следует|подпишись|спасибо за просмотр|перевод|озвуч|"
                  r"dimatorzok|amara|www\.|\.com|♪", re.I)


@dataclass
class SceneCandidate:
    start: float
    end: float
    text: str
    score: float
    title: str = ""
    finale: list[float] = field(default_factory=list)   # кадры для финальной нарезки (время в фильме)


def _norm_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яёa-z]{4,}", text.lower().replace("ё", "е"))}


class ScenePicker:
    def __init__(self, min_len: float = 14.0, max_len: float = 26.0, max_gap: float = 4.0):
        self.min_len, self.max_len, self.max_gap = min_len, max_len, max_gap

    def pick(self, workdir: Path, duration: float, count: int, spoiler_after: float = 0.0,
             quotes: list[str] | None = None, names: list[str] | None = None,
             exclude: list[tuple[float, float]] | None = None) -> list[SceneCandidate]:
        segs = json.loads((workdir / "transcript.json").read_text(encoding="utf-8"))["segments"]
        segs = [s for s in segs if s["text"].strip() and not JUNK.search(s["text"])]
        sig = dict(np.load(workdir / "signals.npz"))
        z = lambda x: (x - np.median(x)) / (np.std(x) + 1e-6)
        loud, motion = z(sig["loud"].astype(float)), z(sig["motion"].astype(float))
        bright = sig["bright"].astype(float) * (255.0 if sig["bright"].max() <= 1.5 else 1.0)   # 0..1 -> 0..255
        lo = max(60.0, duration * 0.03)
        hi = spoiler_after or duration * 0.82
        quote_sets = [_norm_words(q) for q in (quotes or []) if len(_norm_words(q)) >= 3]

        def sig_mean(arr: np.ndarray, a: float, b: float) -> float:
            i, j = int(a * SIGNAL_FPS), max(int(a * SIGNAL_FPS) + 1, int(b * SIGNAL_FPS))
            return float(np.mean(arr[i:j])) if i < len(arr) else 0.0

        cands: list[SceneCandidate] = []
        n = len(segs)
        for i in range(n):
            s0 = segs[i]
            if s0["start"] < lo or s0["start"] > hi:
                continue
            gap_before = s0["start"] - segs[i - 1]["end"] if i else 9.0
            prev_closed = i == 0 or re.search(r"[.!?…]$", segs[i - 1]["text"].strip())
            if gap_before < 0.6 and not prev_closed:
                continue                                    # не начинаем посреди чужой фразы
            for j in range(i, n):
                if j > i and segs[j]["start"] - segs[j - 1]["end"] > self.max_gap:
                    break                                   # долгое молчание — дальше другая сцена
                a, b = s0["start"] - 0.35, segs[j]["end"] + 0.45
                if b - a > self.max_len or b > hi:
                    break
                if b - a < self.min_len or not re.search(r"[.!?…]$", segs[j]["text"].strip()):
                    continue
                gap_after = segs[j + 1]["start"] - segs[j]["end"] if j + 1 < n else 9.0
                if gap_after < 0.35:
                    continue                                # следующая реплика вплотную — мысль не закончена
                text = " ".join(s["text"] for s in segs[i:j + 1])
                speech = sum(s["end"] - s["start"] for s in segs[i:j + 1]) / (b - a)
                turns = j - i + 1
                punch = len(re.findall(r"[!?]", text))
                last = segs[j]
                score = (1.2 * min(speech, 0.9) + 0.08 * min(turns, 10) + 0.12 * min(punch, 6)
                         + 0.35 * sig_mean(loud, a, b) + 0.25 * sig_mean(motion, a, b))
                if last["end"] - last["start"] < 2.5 and re.search(r"[!?]$", last["text"].strip()):
                    score += 0.4                            # короткая финальная реплика — панчлайн
                br = sig_mean(bright, a, b)
                if br < 45:
                    score -= 0.9 * (45 - br) / 20           # тёмная сцена плохо смотрится на телефоне
                words = _norm_words(text)
                if quote_sets and any(len(q & words) >= max(3, 0.5 * len(q)) for q in quote_sets):
                    score += 1.5                            # известная цитата — культовая сцена
                cands.append(SceneCandidate(a, b, text, score))
        cands.sort(key=lambda c: -c.score)
        chosen: list[SceneCandidate] = []
        busy = list(exclude or [])
        for c in cands:
            if all(c.end + 90 < a or c.start - 90 > b for a, b in busy):
                chosen.append(c)
                busy.append((c.start, c.end))
            if len(chosen) >= count:
                break
        for c in chosen:
            c.finale = self.finale_shots(sig, c.start, c.end)
            c.title = self.title(c.text, names or [])
        chosen.sort(key=lambda c: c.start)
        log.info("Автоподбор сцен: %d кандидатов, выбрано %d: %s", len(cands), len(chosen),
                 "; ".join(f"{c.start:.0f}–{c.end:.0f} «{c.title}»" for c in chosen))
        return chosen

    @staticmethod
    def finale_shots(sig: dict, a: float, b: float, k: int = 14) -> list[float]:
        """Кадры финальной нарезки — пики движения ВНУТРИ этой же сцены (плюс небольшой запас по краям)."""
        motion = sig["motion"].astype(float)
        bright = sig["bright"].astype(float) * (255.0 if sig["bright"].max() <= 1.5 else 1.0)   # 0..1 -> 0..255
        i0, i1 = int(max(0, a - 6) * SIGNAL_FPS), int((b + 6) * SIGNAL_FPS)
        idx = np.arange(i0, min(i1, len(motion)))
        if len(idx) == 0:
            return list(np.linspace(a, b, k))
        order = idx[np.argsort(-(motion[idx] + 0.002 * bright[idx]))]
        picked: list[int] = []
        for i in order:
            if bright[i] < 20:
                continue
            if all(abs(i - p) >= 1.6 * SIGNAL_FPS for p in picked):   # кадры не ближе ~1.6 с друг к другу
                picked.append(int(i))
            if len(picked) >= k:
                break
        if len(picked) < k // 2:                                       # мало движения — равномерно по сцене
            picked = list(np.linspace(a * SIGNAL_FPS, b * SIGNAL_FPS, k).astype(int))
        return sorted(p / SIGNAL_FPS for p in picked)

    @staticmethod
    def title(text: str, names: list[str]) -> str:
        """Спокойная подпись-контекст без Claude: герои сцены или её самая короткая яркая фраза."""
        present = [n for n in names if re.search(rf"\b{re.escape(n)}", text)]
        if len(present) >= 2:
            return f"{present[0]} и {present[1]}"
        sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
        good = [s for s in sentences if 12 <= len(s) <= 34 and not s[0].islower()]
        if good:
            best = max(good, key=lambda s: (s.endswith(("!", "?")), -abs(len(s) - 24)))
            return best.rstrip(".…")
        return present[0] if present else "Сцена из фильма"
