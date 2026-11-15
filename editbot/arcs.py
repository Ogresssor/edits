"""Сюжетная линия для эдита (формат «arc», по разбору эталонных каналов) — работает без Claude.

Эталонный эдит ~45–60 с рассказывает одну линию: 4–6 ключевых реплик героев по порядку фильма, между ними
кадры-перебивки под музыку, потом музыка «взрывается» (дроп) и идёт быстрая нарезка самых ярких кадров.
Чтобы кадры были «про одно», линия берётся из одного эпизода фильма (окно ~3 мин: гонка, ссора, погоня).

1. Эпизоды выбираются по черновой расшифровке всего фильма: много речи с восклицаниями/вопросами,
   громко, есть динамичные светлые кадры для нарезки, известные цитаты (агент-исследователь).
2. Лучшие эпизоды расшифровываются заново по словам (точная модель) и режутся на предложения.
   Реплика-кандидат: законченное предложение 0.7–6 с с паузами вокруг (чистый вход и выход).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .scenes import JUNK, SIGNAL_FPS, _norm_words

log = logging.getLogger("arcs")


@dataclass
class ArcCandidate:
    start: float
    end: float
    lines: list[tuple] = field(default_factory=list)          # (начало, конец, текст, оценка)
    broll: list[float] = field(default_factory=list)     # кадры-перебивки (время в фильме), по порядку
    finale: list[float] = field(default_factory=list)    # кадры финальной нарезки
    title: str = ""
    score: float = 0.0
    mood: str = "tension"                                 # настроение — под него подбирается музыка


SENT_END = re.compile(r"[.!?…]$")
# проходные фразы: для эдита в них нет ни конфликта, ни смысла
BANAL = re.compile(r"\b(спасибо|пока|до завтра|до встречи|привет|здравствуй\w*|ладно|хорошо|ага|угу|окей)\b", re.I)

# слова-признаки настроения эпизода (корни; ищутся в репликах линии)
MOOD_WORDS = {
    "tension": ("убь", "убит", "сдохн", "заткн", "провал", "покин", "долг", "угрож", "пистолет", "стреля", "бей",
                "драк", "ненави", "не лезь", "отвал", "смерт", "якудз", "проигра", "предупрежд", "кров", "опасн"),
    "intrigue": ("кто", "почему", "зачем", "что это", "тайн", "секрет", "правд", "знаешь", "откуда", "означает",
                 "странн", "как ты", "кто ты"),
    "hype": ("гонк", "быстр", "газ", "дрифт", "тачк", "машин", "давай", "погнал", "вперёд", "вперед", "скорост",
             "мотор", "движок", "побед", "выигра"),
    "sad": ("прост", "люблю", "умер", "погиб", "потерял", "скучаю", "больно", "один", "плач", "отец", "мам",
            "прощ", "жаль"),
    "fun": ("ха", "шутк", "смешн", "прикол", "весел", "дурак", "лол"),
}


def detect_mood(lines, loud: float = 0.0, motion: float = 0.0, genres: str = "") -> str:
    """Настроение линии по репликам и динамике: tension / intrigue / hype / sad / fun."""
    text = " ".join(t for _, _, t, *_ in lines).lower()
    score = {m: sum(text.count(w) for w in ws) for m, ws in MOOD_WORDS.items()}
    score["intrigue"] += 0.5 * text.count("?")
    score["tension"] += 0.5 * text.count("!") + max(0.0, loud) * 2
    score["hype"] += max(0.0, motion) * 3
    if any(g in genres for g in ("комеди", "comedy")):
        score["fun"] += 2
    if any(g in genres for g in ("драм", "drama")):
        score["sad"] += 1
    return max(score, key=score.get) if max(score.values()) > 0 else "tension"

# крючок: с чего начинается ролик. По статистике канала решают первые 1.5 с: «Что за идиот?» держит 76%
# зрителей, медленное «О… господи… ну когда ты успокоишься?» — 61% (и YouTube перестаёт показывать ролик)
HOOK_WORDS = ("идиот", "дурак", "псих", "заткн", "стой", "стоять", "руки", "на пол", "убь", "убит", "чёрт", "черт",
              "проблем", "полиц", "коп", "пушк", "ствол", "вали", "провалива", "какого", "что ты делаешь", "кто ты",
              "ты кто", "эй", "осторожн", "врёшь", "врешь", "заливай", "сдвинул", "ненормальн", "козёл", "козел",
              "ублюд", "свинь", "урод", "сволоч", "ну-ка", "живо", "быстро", "не смей", "отпусти", "отвали", "тачк")
_FILLER_START = ("о,", "о ", "ну ", "ну,", "а ", "э", "так,", "так ", "да ", "да,", "мм", "хм")


def hook_score(text: str) -> float:
    """Насколько реплика годится в первые секунды ролика: суть — в первой короткой фразе, вопрос или
    восклицание, конфликт (оскорбление, приказ, угроза). Выше 0.8 — хороший крючок."""
    t = text.strip()
    first = re.split(r"(?<=[.!?…])\s+", t, maxsplit=1)[0]
    low, n = first.lower(), len(first.split())
    s = 0.6 if first.endswith(("?", "!")) else 0.0
    s += 0.5 if n <= 5 else 0.2 if n <= 8 else -0.3
    s += 0.8 if any(w in low for w in HOOK_WORDS) else 0.0
    s -= 0.3 if low.startswith(_FILLER_START) else 0.0
    s -= 0.6 if BANAL.search(first) else 0.0
    return round(s, 2)


_CONT_WORDS = {"и", "а", "но", "или", "что", "чтобы", "потому", "если", "когда", "как", "да", "ведь", "зато",
               "and", "but", "or", "because", "so", "that"}


def continues(prev_text: str, next_text: str) -> bool:
    """Следующие слова продолжают мысль: предыдущее слово без знака конца, следующее со строчной буквы
    или предыдущее/следующее — союз («проблемы вам ... и себе»)."""
    p = prev_text.strip()
    n = next_text.strip()
    if not n:
        return False
    if p.rstrip(",;:—–-").lower() in _CONT_WORDS or n.lower().strip(",.") in _CONT_WORDS:
        return True
    if n[:1].islower():
        return True
    return not re.search(r"[.!?…]$", p)


def exchanges(words, gap: float = 1.0, max_len: float = 10.0) -> list[tuple[float, float, str]]:
    """Слова -> законченные «обмены»: речь подряд (паузы короче gap — продолжение фразы или ответ
    собеседника) между настоящими паузами. Точки Whisper на длинном куске ставит ненадёжно, поэтому
    границы — по паузам. Обмен длиннее max_len делим только в конце предложения с паузой;
    если так поделить нельзя — не берём (резать посреди мысли нельзя)."""
    groups, cur = [], []
    for w in words:
        pause = w.start - cur[-1].end if cur else 0.0
        joined = bool(cur) and ((pause < 2.0 and continues(cur[-1].text, w.text))
                                or (pause < 2.5 and cur[-1].text.endswith("?")))   # вопрос и ответ — вместе
        if cur and pause >= gap and not joined:
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        pieces, start = [], 0
        while start < len(g):
            if g[-1].end - g[start].start <= max_len:
                pieces.append(g[start:])
                break
            cut = None
            for k in range(start, len(g) - 1):
                if g[k].end - g[start].start > max_len:
                    break
                if SENT_END.search(g[k].text) and g[k + 1].start - g[k].end >= 0.35:
                    cut = k
            if cut is None:
                pieces = []
                break
            pieces.append(g[start:cut + 1])
            start = cut + 1
        out += [(p[0].start, p[-1].end, " ".join(w.text for w in p)) for p in pieces]
    return out


def sentences(words) -> list[tuple[float, float, str]]:
    """Слова -> предложения (по знакам конца или паузе > 0.6 с)."""
    out, cur = [], []
    for w in words:
        if cur and w.start - cur[-1].end > 0.6:
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w.text):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [(s[0].start, s[-1].end, " ".join(w.text for w in s)) for s in out]


class ArcPicker:
    def __init__(self, window: float = 180.0, n_lines: int = 5, min_lines: int = 3, budget: float = 30.0,
                 line_max: float = 10.0, hook_min: float = 0.0, scene: bool = False):
        # budget — сколько секунд реплик влезает в историю (остальное — перебивки), реплики берутся целиком
        self.window, self.n_lines, self.min_lines, self.budget = window, n_lines, min(min_lines, n_lines), budget
        self.line_max = line_max
        self.hook_min = hook_min
        # scene: одна цельная сцена — реплики подряд, без пропусков середины (как у эталонов: завязка → развязка)
        self.scene = scene

    def pick(self, workdir: Path, duration: float, count: int, transcribe: Callable[[float, float], list],
             spoiler_after: float = 0.0, quotes: list[str] | None = None,
             names: list[str] | None = None, genres: list[str] | None = None,
             avoid: list[tuple[float, float]] | None = None) -> list[ArcCandidate]:
        """avoid — отрезки, уже взятые в прошлые ролики этого фильма (в другом формате): их не повторяем."""
        segs = json.loads((workdir / "transcript.json").read_text(encoding="utf-8"))["segments"]
        segs = [s for s in segs if s["text"].strip() and not JUNK.search(s["text"])]
        sig = dict(np.load(workdir / "signals.npz"))
        z = lambda x: (x - np.median(x)) / (np.std(x) + 1e-6)
        self.loud, self.motion = z(sig["loud"].astype(float)), z(sig["motion"].astype(float))
        self.bright = sig["bright"].astype(float) * (255.0 if sig["bright"].max() <= 1.5 else 1.0)
        lo = max(60.0, duration * 0.03)
        hi = spoiler_after or duration * 0.85
        self.quotes = [_norm_words(q) for q in (quotes or []) if len(_norm_words(q)) >= 3]
        self.names = names or []
        self.genres = " ".join(genres or []).lower()

        # 1) эпизоды по черновой расшифровке
        cands: list[ArcCandidate] = []
        for w0 in np.arange(lo, max(lo + 1, hi - self.window * 0.5), 15.0):
            w1 = min(hi, w0 + self.window)
            inside = [s for s in segs if w0 <= s["start"] and s["end"] <= w1]
            text = " ".join(s["text"] for s in inside)
            n_sent = len(re.findall(r"[.!?…](\s|$)", text))
            if n_sent < self.min_lines + 2:
                continue
            speech = sum(s["end"] - s["start"] for s in inside) / (w1 - w0)
            peaks = self._peaks(w0, w1, [], 60)
            score = (0.6 * min(n_sent, 25) / 25 + 0.5 * min(len(re.findall(r"[!?]", text)), 12) / 12
                     + 0.5 * min(speech, 0.6) + 0.5 * min(len(peaks), 40) / 40
                     + 0.25 * self._mean(self.loud, w0, w1) + 0.25 * self._mean(self.motion, w0, w1)
                     - 0.5 * max(0.0, 45 - self._mean(self.bright, w0, w1)) / 20)
            words = _norm_words(text)
            score += 0.8 * sum(1 for q in self.quotes if len(q & words) >= max(3, 0.5 * len(q)))
            cands.append(ArcCandidate(float(w0), float(w1), score=float(score)))
        cands.sort(key=lambda c: -c.score)

        # 2) лучшие эпизоды — точная расшифровка по словам и выбор реплик
        picked: list[ArcCandidate] = []
        tried = 0
        for c in cands:
            if len(picked) >= count or tried >= count + 12:
                break
            if not all(c.end + 30 < p.start or c.start - 30 > p.end for p in picked):
                continue
            if any(not (c.end < a0 or c.start > a1) for a0, a1 in (avoid or [])):
                continue
            tried += 1
            words = transcribe(c.start, c.end - c.start)
            c.lines = self._lines(exchanges(words), c.start, c.end)
            if len(c.lines) < self.min_lines:
                log.info("  эпизод %.0f–%.0f: мало чистых реплик (%d) — пропускаю", c.start, c.end, len(c.lines))
                continue
            # крючок: ролик начинается с сильной реплики — вялые реплики в начале сцены отбрасываем
            k = 0
            while k < len(c.lines) - self.min_lines and hook_score(c.lines[k][2]) < self.hook_min:
                k += 1
            if hook_score(c.lines[k][2]) < self.hook_min:
                log.info("  эпизод %.0f–%.0f: нет сильного начала («%s») — пропускаю", c.start, c.end,
                         c.lines[k][2][:40])
                continue
            c.lines = c.lines[k:]
            # перебивки и финал — из той же части эпизода, где звучат реплики (кадры «про одно»)
            peaks = self._peaks(c.lines[0][0] - 12, c.lines[-1][1] + 12, c.lines, 48)
            if len(peaks) < 20:
                peaks = self._peaks(c.lines[0][0] - 25, c.lines[-1][1] + 25, c.lines, 48)
            by_motion = sorted(peaks, key=lambda t: -self.motion[int(t * SIGNAL_FPS)])
            c.finale = sorted(by_motion[:16])
            c.broll = sorted(t for t in peaks if t not in c.finale)[:28] or sorted(peaks)
            c.title = self.title(c.lines)
            c.mood = detect_mood(c.lines, self._mean(self.loud, c.start, c.end), self._mean(self.motion, c.start, c.end),
                                 self.genres)
            picked.append(c)
        picked.sort(key=lambda c: c.start)
        log.info("Сюжетные линии: %d эпизодов-кандидатов, выбрано %d: %s", len(cands), len(picked),
                 "; ".join(f"{c.start:.0f}–{c.end:.0f} ({len(c.lines)} реплик, {c.mood}) «{c.title}»" for c in picked))
        return picked

    # ------------------------------------------------------------------
    def _mean(self, arr, a: float, b: float) -> float:
        i, j = int(a * SIGNAL_FPS), max(int(a * SIGNAL_FPS) + 1, int(b * SIGNAL_FPS))
        return float(np.mean(arr[i:j])) if i < len(arr) else 0.0

    def _lines(self, sents: list[tuple[float, float, str]], w0: float, w1: float) -> list[tuple[float, float, str]]:
        cand = []
        for i, (a, b, text) in enumerate(sents):
            text = text.strip()
            if not (1.0 <= b - a <= self.line_max) or not (5 <= len(text) <= 220) or JUNK.search(text):
                continue
            gap_a = sents[i + 1][0] - b if i + 1 < len(sents) else 9.0
            if not SENT_END.search(text) and gap_a < 1.5:
                continue                                  # мысль не закончена — дальше её продолжают
            if text.rstrip().endswith(("...", "…")) and gap_a < 2.0:
                continue
            n_words = len(text.split())
            sc = (0.3 * min(3, len(re.findall(r"[!?]", text))) + 0.35 * self._mean(self.loud, a, b)
                  + 0.15 * self._mean(self.motion, a, b)
                  + (0.4 if 2.0 <= b - a <= 7.0 else 0.0)            # законченный обмен, но не монолог
                  + (0.3 if n_words >= 4 else -0.4)                   # «Да.» «Что?» — не реплика для эдита
                  + (0.3 if n_words >= 9 else 0.0)                    # содержательный обмен, а не «Пока.»
                  - 0.3 * min(2, len(BANAL.findall(text)))            # «Спасибо. До завтра. Привет» — проходное
                  + 0.3 * max(0.0, hook_score(text)))                 # годится, чтобы начать ролик
            words = _norm_words(text)
            if self.quotes and any(len(q & words) >= max(3, 0.5 * len(q)) for q in self.quotes):
                sc += 1.5
            if any(re.search(rf"\b{re.escape(n)}", text) for n in self.names):
                sc += 0.25
            br = self._mean(self.bright, a, b)
            if br < 40:
                sc -= 0.5 * (40 - br) / 20
            cand.append((a, b, text[:1].upper() + text[1:], sc))
        if self.scene:
            return self._run(cand)
        chosen: list[tuple] = []
        for ln in sorted(cand, key=lambda x: -x[3]):
            if sum(c[1] - c[0] for c in chosen) + (ln[1] - ln[0]) > self.budget:
                continue                                  # не влезет целиком — берём другую
            if all(ln[1] + 2.0 < c[0] or ln[0] > c[1] + 2.0 for c in chosen):
                chosen.append(ln)
            if len(chosen) >= self.n_lines:
                break
        return [(round(a, 2), round(b, 2), t, round(float(sc), 2)) for a, b, t, sc in sorted(chosen)]

    def _run(self, cand: list[tuple], max_gap: float = 4.0) -> list[tuple]:
        """Лучший отрезок реплик подряд (между соседними не больше max_gap с, всё вместе — в бюджет речи):
        сцена целиком, а не лучшие реплики вразброс."""
        cand = sorted(cand)
        best, best_sc = [], -1e9
        for i in range(len(cand)):
            run, total = [], 0.0
            for j in range(i, len(cand)):
                a, b, _t, _sc = cand[j]
                if run and a - run[-1][1] > max_gap or total + (b - a) > self.budget:
                    break
                run.append(cand[j])
                total += b - a
                if len(run) >= self.min_lines:
                    # сумма оценок + доля речи: длинная насыщенная сцена лучше двух сильных фраз
                    sc = sum(x[3] for x in run) + 0.1 * total + 0.5 * max(0.0, hook_score(run[0][2]))
                    if sc > best_sc:
                        best, best_sc = list(run), sc
        return [(round(a, 2), round(b, 2), t, round(float(sc), 2)) for a, b, t, sc in best]

    def _peaks(self, a: float, b: float, lines, k: int) -> list[float]:
        """Яркие динамичные кадры эпизода (не на репликах), не ближе 1.5 с друг к другу."""
        motion, bright = self.motion, self.bright
        i0, i1 = int(max(0.0, a) * SIGNAL_FPS), min(len(motion), int(b * SIGNAL_FPS))
        idx = np.arange(i0, i1)
        if len(idx) == 0:
            return []
        order = idx[np.argsort(-motion[idx])]
        out: list[int] = []
        for i in order:
            t = i / SIGNAL_FPS
            if bright[i] < 25 or any(la - 0.6 <= t <= lb + 0.6 for la, lb, *_ in lines):
                continue
            if all(abs(i - p) >= 1.5 * SIGNAL_FPS for p in out):
                out.append(int(i))
            if len(out) >= k:
                break
        return sorted(p / SIGNAL_FPS for p in out)

    @staticmethod
    def title(lines: list[tuple[float, float, str]]) -> str:
        """Подпись-плашка без Claude: самая короткая сильная реплика линии + многоточие."""
        sents = []
        for _, _, text, *_ in lines:
            text = re.sub(r"\s+-(то|либо|нибудь|ка)\b", r"-\1", text)      # «что -нибудь» -> «что-нибудь»
            text = re.sub(r"(\w)\s+(['’]\w)", r"\1\2", text)                # «О 'Коннор» -> «О'Коннор»
            parts = [s.strip(" –-") for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip(" –-")]
            # «Победитель. Получит. Меня.» — Whisper расставил точки между словами: склеиваем обрывки
            merged: list[str] = []
            for s in parts:
                # только обрывки с обеих сторон: «Джесси.» + «Ну, Брайан, готов?» — это две фразы, не одна
                if merged and len(merged[-1].split()) <= 2 and len(s.split()) <= 2 \
                        and not merged[-1].endswith(("?", "!")):
                    merged[-1] = merged[-1].rstrip(".…") + " " + s[:1].lower() + s[1:]
                else:
                    merged.append(s)
            sents += merged
        good = [t for t in sents if 12 <= len(t) <= 42 and len(t.split()) >= 3] or sents or ["…"]
        best = max(good, key=lambda t: (t.endswith(("!", "?")), -abs(len(t) - 26)))
        best = best[:1].upper() + best[1:]
        return best if best.endswith("?") else best.rstrip(".!…") + "…"
