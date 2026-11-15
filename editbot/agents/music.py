"""Агент-музыкант: подбирает и скачивает свободную музыку под настроение эдита.

Источник — Openverse (api.openverse.org): открытый каталог Creative Commons-аудио. Сначала ищем среди
полноценных треков (Jamendo, Wikimedia — Kevin MacLeod, Rafael Krux и др.), Freesound — запасной вариант.
Берутся только лицензии, разрешающие использование в видео, в том числе коммерческом: CC0, Public Domain Mark,
CC BY. Для CC BY обязательно указание автора — агент сам формирует строку для описания ролика.
Из нескольких найденных треков выбирается тот, у которого самый чёткий бит и подходящий темп.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from .. import ffmpeg
from ..config import Config
from ..state import State
from .curator import EditPlan

log = logging.getLogger("music")

MUSIC_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
OPENVERSE = "https://api.openverse.org/v1/audio/"
UA = {"User-Agent": "editbot/1.0 (personal movie-edit generator; python-requests)"}

# Современная музыка под настроение эдита-линии (настроение определяет editbot/arcs.py по репликам и динамике)
MOOD_QUERIES = {   # запросы, по которым в каталоге реально есть треки (проверено); жанр под настроение — по меткам
    "tension": ["dark", "industrial", "horror", "drum and bass", "dubstep", "trap", "tension", "action"],
    "intrigue": ["mysterious", "mystery", "noir", "dark", "investigation", "suspense", "dark ambient", "horror"],
    "hype": ["action", "drum and bass", "drift", "beat", "dubstep", "trap", "epic", "industrial"],
    "sad": ["sad", "emotional", "melancholic", "piano", "dark ambient"],
    "fun": ["funky", "groove", "hip hop", "beat"],
}
# жанры, подходящие / неуместные под настроение (метки Jamendo)
MOOD_GENRES = {
    "tension": ({"phonk", "trap", "dubstep", "darkwave", "industrial", "darkambient", "bass", "drumnbass", "dnb",
                 "darksynth", "cyberpunk", "grime", "drill", "hardstyle", "dark", "breakbeat"},
                {"electropop", "house", "chillout", "pop", "surf", "surfrock", "happy", "lounge", "funk", "disco"}),
    "intrigue": ({"darkambient", "triphop", "downtempo", "ambient", "trap", "darkwave", "experimental", "idm",
                  "minimal", "dark", "lofi", "cinematic", "soundtrack"},
                 {"electropop", "house", "happy", "pop", "surf", "funk", "disco", "dance"}),
    "hype": ({"phonk", "trap", "dubstep", "drumnbass", "dnb", "hardstyle", "edm", "electro", "bigbeat", "breakbeat",
              "techno", "hardcore", "rock", "metal"},
             {"chillout", "ambient", "lounge", "lofi", "downtempo", "piano"}),
    "sad": ({"lofi", "piano", "ambient", "downtempo", "chillout", "triphop", "cinematic", "soundtrack", "emotional"},
            {"hardstyle", "dubstep", "metal", "hardcore", "happy", "disco", "funk"}),
    "fun": ({"funk", "hiphop", "groove", "electroswing", "disco", "happy", "upbeat", "pop"},
            {"darkambient", "industrial", "darkwave", "metal"}),
}
MODERN = {"phonk", "trap", "hiphop", "hip-hop", "rap", "beat", "beats", "electronic", "electro", "dubstep", "bass",
          "drumandbass", "dnb", "edm", "lofi", "lo-fi", "synthwave", "darksynth", "cyberpunk", "grime", "drill",
          "triphop", "breakbeat", "industrial", "darkwave", "instrumental"}

# Запасные запросы, если по запросу куратора ничего не нашлось
DEFAULT_QUERIES = {"scene": ["dark epic", "cinematic", "suspense"], "montage": ["epic trailer", "action", "epic"]}
# Сначала «настоящая» музыка, потом библиотеки звуков
SOURCE_TIERS = ["jamendo,wikimedia_audio", "freesound"]
EXT_MAP = {".mp32": ".mp3", ".ogx": ".ogg", ".oga": ".ogg"}
# Жанры/теги Jamendo: киномузыка в приоритете, песни с вокалом и «радийные» жанры — мимо
CINEMATIC = {"filmscore", "soundtrack", "cinematic", "trailer", "epic", "orchestra", "orchestral", "score",
             "epicdrums", "instrumental", "ambient", "classical", "atmospheric", "dark", "dramatic"}
REJECT_GENRES = {"pop", "poprock", "rap", "hiphop", "dance", "house", "techno", "reggae", "country", "schlager",
                 "rnb", "latin", "chanson", "disco", "gypsy"}
REJECT_TAGS = {"vocal", "male", "female", "vocals", "singer", "lyrics"}


def _labels(item: dict) -> tuple[set[str], set[str]]:
    genres = {str(g).lower() for g in (item.get("genres") or [])}
    tags = {str(t.get("name", "") if isinstance(t, dict) else t).lower() for t in (item.get("tags") or [])}
    return genres, tags


MODERN_REJECT = {"country", "schlager", "chanson", "reggae", "latin", "gypsy", "disco", "polka", "folk",
                 "children", "christmas", "religious", "gospel", "classical", "baroque", "jazz", "blues", "swing"}


def modern_score(item: dict, query: str, mood: str = "") -> float:
    """Современный трек для эдита (phonk/trap/electronic…): вокал и «не те» жанры — мимо. <0 — не подходит."""
    genres, tags = _labels(item)
    labels = genres | tags
    title = (item.get("title") or "").lower()
    if tags & REJECT_TAGS or genres & MODERN_REJECT or any(w in title for w in ("feat.", "ft.", "remix vocal")):
        return -1.0
    score = 1.0 + 0.4 * min(2, len(labels & MODERN))
    score += 0.4 * sum(1 for w in query.lower().split() if w in labels or w in title)
    good, bad = MOOD_GENRES.get(mood, (set(), set()))
    score += 0.8 * min(2, len(labels & good)) - 0.9 * min(2, len(labels & bad))   # под настроение — главное
    return max(score, 0.05)


def fit_score(item: dict, query: str, modern: bool = False, mood: str = "") -> float:
    """Насколько трек подходит как музыка для эдита (по жанрам/тегам/названию). <0 — не подходит."""
    if modern:
        return modern_score(item, query, mood)
    genres, tags = _labels(item)
    labels = genres | tags
    if genres & REJECT_GENRES or tags & REJECT_TAGS:
        return -1.0  # вокал перебивает реплики фильма, «радийные» жанры не звучат как кино
    title = (item.get("title") or "").lower()
    score = 1.0
    score += 0.6 * min(2, len(labels & CINEMATIC))
    score += 0.4 * any(w in title for w in ("cinematic", "trailer", "epic", "score", "soundtrack", "theme"))
    score += 0.3 * sum(1 for w in query.lower().split() if w in labels)
    return score


# ---------------------------------------------------------------- анализ трека
@dataclass
class Beats:
    period: float     # секунд на долю
    offset: float     # с какого места трека начинать (ровно на доле)
    strength: float   # чёткость бита 0..1
    loudness: float   # средняя громкость выбранного куска, дБ


def analyze_track(path: Path, need: float) -> Beats:
    """Бит-трекер: спектральный поток -> темп по автокорреляции -> самый мощный кусок -> фаза сетки."""
    sr, n_fft, hop = 11025, 1024, 256
    proc = ffmpeg.popen_raw(["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    assert proc.stdout
    y = np.frombuffer(proc.stdout.read(), np.float32)
    proc.wait()
    if len(y) < sr * 5:
        return Beats(0.5, 0.0, 0.0, -60.0)

    frames = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop]
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-6
    spec = np.log1p(np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1)))
    flux = np.maximum(0, np.diff(spec, axis=0)).sum(axis=1)
    flux = (flux - flux.mean()) / (flux.std() + 1e-9)
    fps = sr / hop
    # резкость атак: средний уровень 10% самых сильных атак (у пэдов и эмбиента низкий)
    attack = float(np.sort(flux)[-max(1, len(flux) // 10):].mean())

    # темп: 70–180 BPM, лёгкое предпочтение ~120
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    lags = np.arange(int(fps * 60 / 180), int(fps * 60 / 70))
    weights = np.exp(-0.5 * (np.log2((60 * fps / lags) / 120) / 0.9) ** 2)
    lag = int(lags[np.argmax(ac[lags] * weights)])
    # чёткость пульса: насколько пик автокорреляции на темпе выделяется над фоном,
    # умноженная на резкость атак
    med = float(np.median(ac[lags]))
    pulse = (ac[lag] - med) / (ac[0] - med + 1e-9)
    strength = float(np.clip(pulse, 0, 1) * np.clip((attack - 1.2) / 1.0, 0, 1))
    # уточняем период до долей кадра по пику автокорреляции на 8-кратном лаге,
    # иначе за 40 с склейки уплывают от бита
    period = lag / fps
    far = lag * 8
    if far + 4 < len(ac):
        win = np.arange(far - 4, far + 5)
        k = int(win[np.argmax(ac[win])])
        a, b, c = ac[k - 1], ac[k], ac[k + 1]
        denom = a - 2 * b + c
        period = (k + (0.5 * (a - c) / denom if denom < 0 else 0.0)) / 8 / fps

    # самый мощный кусок длиной need (кульминация трека), если трек длиннее
    n_need = int(need * fps)
    total = len(flux) / fps
    start_f = 0
    if len(rms) > n_need + int(4 * fps):
        csum = np.concatenate([[0.0], np.cumsum(rms[: len(flux)])])
        sums = csum[n_need:] - csum[:-n_need]
        # чуть раньше пика энергии: эдит должен «влететь» в кульминацию, а не начаться с неё
        best = int(np.argmax(sums))
        start_f = max(0, best - int(2 * fps))
    # фаза сетки в выбранном куске
    pf = period * fps
    beats_n = max(4, int(min(need, total) / period) - 1)
    def grid_score(p: float) -> float:
        idx = np.round(start_f + p + np.arange(beats_n) * pf).astype(int)
        idx = idx[idx < len(flux)]
        return float(flux[idx].sum())
    phase = max(np.arange(0, pf, 0.5), key=grid_score)
    offset = (start_f + phase) / fps
    seg = rms[start_f: start_f + n_need] if n_need else rms
    loudness = float(20 * np.log10(np.mean(seg)))
    return Beats(float(period), float(offset), strength, loudness)


# ---------------------------------------------------------------- агент
@dataclass
class Track:
    path: Path
    title: str
    creator: str
    license: str
    url: str          # страница трека (для указания автора)
    credit: str       # готовая строка для описания
    beats: Beats


def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text)[:50]


class MusicAgent:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.m = cfg.music
        self.state = state
        self.dir = cfg.paths.path("music")
        self.auto_dir = self.dir / "auto"
        self.auto_dir.mkdir(parents=True, exist_ok=True)
        self._throttled: set[str] = set()  # источники, ответившие «слишком много запросов»
        self._offline = False  # поиск Openverse не отвечает — до конца запуска берём уже скачанные треки
        self.cleaner = None  # уборщик (editbot/cleanup.py): удаляет не выбранных кандидатов
        self.hints: list[str] = []  # запросы под жанр фильма от агента-исследователя
        self.film_used: set[str] = set()  # треки, уже стоящие в эдитах этого фильма (не повторяем)
        self._mood = ""

    def pick(self, plan: EditPlan, need: float) -> Track | None:
        if not plan.use_music or self.m.source == "none":
            if not plan.use_music:
                log.info("Куратор решил: этот ролик без музыки")
            return None
        if self.m.source == "auto" and plan.kind == "arc":
            # эдит-линия: современная музыка под настроение (напряжение, интрига, драйв, грусть, юмор)
            mood = getattr(plan, "mood", "") or "tension"
            queries = list(MOOD_QUERIES.get(mood, MOOD_QUERIES["tension"]))
            log.info("Музыка: настроение эдита — %s", mood)
            # не нашлось под настроение (источник ограничил скачивания, всё уже стоит в этом фильме) —
            # пробуем соседние настроения: эдит без музыки — тихий финал, так нельзя
            for m in dict.fromkeys([mood, "tension", "hype", "intrigue"]):
                if self._offline:
                    break
                try:
                    self._mood = m
                    track = self._pool(list(MOOD_QUERIES.get(m, MOOD_QUERIES["tension"])), need, plan.kind)
                    if track:
                        return track
                except requests.RequestException as e:
                    log.warning("Openverse недоступен (%s)", e)
                log.info("Музыка: под настроение «%s» не нашлось — пробую другое", m)
            self._mood = mood
            log.warning("Не нашёл подходящую музыку онлайн — беру из уже скачанных (music/auto)")
            return self._from_cache(need, plan.kind) or self._from_folder(need)
        if self.m.source == "auto":
            words = [w for w in (plan.music_query or "").lower().split() if w != "cinematic"]
            # «fantasy magic» -> «fantasy magic cinematic» -> «fantasy cinematic» -> «fantasy magic» -> «fantasy»
            queries = [" ".join(words[:k] + ["cinematic"]) for k in (len(words), 1) if words[:k]]
            queries += [" ".join(words[:k]) for k in (len(words), 2, 1) if words[:k]]
            queries += self.hints  # жанр фильма (исследователь) — до общих запасных запросов
            queries += DEFAULT_QUERIES["scene" if plan.kind == "story" else "montage"]
            try:
                for source in SOURCE_TIERS:
                    for q in dict.fromkeys(queries):
                        track = self._from_openverse(q, need, source, plan.kind)
                        if track:
                            return track
            except requests.RequestException as e:
                log.warning("Openverse недоступен (%s)", e)
            log.warning("Не нашёл подходящую музыку онлайн — пробую папку music/")
        return self._from_folder(need)

    # ---------- пул кандидатов по нескольким запросам (эдит-линия) ----------
    def _pool(self, queries: list[str], need: float, kind: str) -> Track | None:
        """Ищет по нескольким запросам настроения, собирает пул, скачивает лучших кандидатов и выбирает трек
        с чётким битом. Уже использованные треки — в самом конце очереди, треки этого фильма — никогда."""
        seen: dict[str, dict] = {}
        for q in queries:
            if self._offline:
                break
            for source in ("jamendo", "jamendo,wikimedia_audio,ccmixter"):
                try:
                    items = self._search(q, need, source, modern=True, mood=self._mood)
                except (requests.Timeout, requests.ConnectionError) as e:
                    # сервис не отвечает — не ждём по 30 с на каждый запрос (21.10: 25 мин на ролик впустую)
                    log.warning("  поиск музыки не отвечает (%s) — до конца запуска беру уже скачанные треки", e)
                    self._offline = True
                    break
                except requests.RequestException as e:
                    log.info("  поиск «%s» в %s не удался: %s", q, source, e)
                    continue
                for it in items:
                    if it["id"] not in seen:
                        it["_q"] = q
                        seen[it["id"]] = it
                if len(seen) >= 3 * int(self.m.candidates):
                    break
            if len(seen) >= 3 * int(self.m.candidates):
                break
        items = [it for it in seen.values() if f"ov:{it['id']}" not in self.film_used]
        log.info("Музыка: в пуле %d треков (%s)", len(items), ", ".join(queries[:3]))
        if not items:
            return None
        import random
        random.shuffle(items)                       # разнообразие при равных оценках
        items.sort(key=lambda it: (self.state.music_use_count(f"ov:{it['id']}") > 0, -it.get("_fit", 1.0)))
        batch = items[: int(self.m.candidates)]
        workers = 1 if any(it.get("source") == "wikimedia_audio" for it in batch) else 6
        with ThreadPoolExecutor(max_workers=workers) as pool:
            paths = list(pool.map(self._download, batch))
        best = None
        for item, path in zip(batch, paths):
            if not path:
                continue
            try:
                beats = analyze_track(path, need)
            except Exception as e:
                log.info("  не разобрал «%s»: %s", item.get("title"), e)
                continue
            used = self.state.music_use_count(f"ov:{item['id']}")
            score = (0.4 + self._score(beats, kind)) * item.get("_fit", 1.0) / (1 + 3 * used)
            log.info("  %-40s %3.0f BPM, бит %.2f, %s, оценка %.2f", (item.get("title") or "")[:40],
                     60 / beats.period, beats.strength, ", ".join(sorted(_labels(item)[0]))[:30], score)
            if best is None or score > best[0]:
                best = (score, item, path, beats)
        if self.cleaner:
            self.cleaner.music_candidates(best[2] if best else None, [p for p in paths if p])
        if not best or best[0] < float(self.m.min_beat_strength) * 0.5:
            return None
        _, item, path, beats = best
        label, credit = self._credit(item)
        self.state.mark_music_used(f"ov:{item['id']}")
        self.film_used.add(f"ov:{item['id']}")
        log.info("Выбран трек «%s» — %s (%s), %.0f BPM, запрос «%s»", item.get("title"), item.get("creator"), label,
                 60 / beats.period, item.get("_q"))
        return Track(path, item.get("title") or "", item.get("creator") or "", label,
                     item.get("foreign_landing_url") or "", credit, beats)

    # ---------- Openverse ----------
    def _search(self, query: str, need: float, source: str, modern: bool = False, mood: str = "") -> list[dict]:
        # без ключа Openverse отдаёт максимум 20 результатов на страницу
        results: list[dict] = []
        for page in (1, 2):
            params = {"q": query, "license": self.m.licenses, "source": source, "page_size": 20, "page": page,
                      "mature": "false"}
            r = requests.get(OPENVERSE, params=params, headers=UA, timeout=30)
            if r.status_code in (400, 401, 404) and page > 1:
                break
            r.raise_for_status()
            data = r.json()
            results += data.get("results", [])
            if page >= (data.get("page_count") or 1):
                break
        out = []
        max_len = float(self.m.max_track_minutes) * 60
        for item in results:
            dur = (item.get("duration") or 0) / 1000
            title = (item.get("title") or "").lower()
            if not item.get("url") or dur < max(need + 5, 50) or dur > max_len:
                continue
            if any(w in title for w in ("sfx", "sound effect", "ambience", "field recording", " pad", "drone",
                                        "noise", "texture", "riser", "whoosh", "logo", "intro",
                                        # любительские конструкторы и 8-бит не звучат как кино
                                        "music2000", "ps1", "chiptune", "8-bit", "8bit", "midi")):
                continue
            item["_fit"] = fit_score(item, query, modern, mood)
            if item["_fit"] > 0:
                out.append(item)
        # сначала самые «киношные»; сортировка устойчивая — внутри равных остаётся релевантность Openverse
        out.sort(key=lambda it: -it["_fit"])
        return out

    def _download(self, item: dict) -> Path | None:
        ext = "." + (item.get("filetype") or "mp3").lower().lstrip(".")
        ext = EXT_MAP.get(ext, ext)
        ext = ext if ext in MUSIC_EXT else ".mp3"
        path = self.auto_dir / f"{item.get('source', 'ov')}_{_slug(item['id'])}{ext}"
        meta = path.with_suffix(".json")
        if not path.exists():
            host = item.get("source", "")
            if host in self._throttled:
                return None
            # Wikimedia ограничивает частоту скачиваний: при 429 коротко ждём, а если не помогло —
            # больше не трогаем этот источник до конца запуска (берём треки из других)
            for attempt in range(2):
                try:
                    with requests.get(item["url"], headers=UA, timeout=60, stream=True) as r:
                        if r.status_code == 429:
                            if attempt == 0:
                                time.sleep(4.0)
                                continue
                            self._throttled.add(host)
                            log.info("  %s ограничивает скачивания — до конца запуска беру музыку из других источников",
                                     host)
                            return None
                        r.raise_for_status()
                        tmp = path.with_suffix(".part")
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_content(1 << 16):
                                f.write(chunk)
                        tmp.replace(path)
                        break
                except requests.RequestException as e:
                    log.info("  не скачался «%s»: %s", item.get("title"), e)
                    return None
            else:
                return None
            if path.exists() and item.get("source") == "wikimedia_audio":
                time.sleep(1.0)
        meta.write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    @staticmethod
    def _credit(item: dict) -> tuple[str, str]:
        lic = (item.get("license") or "").lower()
        ver = item.get("license_version") or ""
        label = {"cc0": "CC0", "pdm": "Public Domain"}.get(lic, f"CC {lic.upper()} {ver}".strip())
        title = item.get("title") or "Untitled"
        creator = item.get("creator") or "unknown"
        page = item.get("foreign_landing_url") or item.get("url")
        return label, f"Музыка: «{title}» — {creator} ({label}) {page}"

    def _score(self, b: Beats, kind: str) -> float:
        bpm = 60 / b.period
        loud_ok = 1.0 if b.loudness > -30 else 0.5
        if kind == "story":
            # подложка под сцену: ритм желателен, но важнее, чтобы трек был «живой», а не тишина
            return (0.4 + b.strength) * loud_ok
        # для нарезки под бит: чёткий ритм 85–150 BPM (полу-/двойной темп тоже режется нормально)
        tempo_ok = 1.0 if 85 <= bpm <= 150 else 0.6
        return b.strength * tempo_ok * loud_ok

    def _from_openverse(self, query: str, need: float, source: str, kind: str) -> Track | None:
        items = self._search(query, need, source)
        log.info("Музыка: «%s» в %s — найдено %d треков", query, source, len(items))
        if not items:
            return None
        # реже использованные — вперёд, затем порядок релевантности Openverse
        items.sort(key=lambda it: (self.state.music_use_count(f"ov:{it['id']}"), -it.get("_fit", 1.0)))
        best: tuple[float, dict, Path, Beats] | None = None
        batch = items[: int(self.m.candidates)]
        # Wikimedia не любит параллельные скачивания — с неё качаем по одному
        workers = 1 if any(it.get("source") == "wikimedia_audio" for it in batch) else 6
        with ThreadPoolExecutor(max_workers=workers) as pool:
            paths = list(pool.map(self._download, batch))
        for item, path in zip(batch, paths):
            if not path:
                continue
            try:
                beats = analyze_track(path, need)
            except Exception as e:
                log.info("  не разобрал «%s»: %s", item.get("title"), e)
                continue
            score = self._score(beats, kind) * item.get("_fit", 1.0) \
                / (1 + self.state.music_use_count(f"ov:{item['id']}"))
            log.info("  %-45s %3.0f BPM, бит %.2f, оценка %.2f",
                     (item.get("title") or "")[:45], 60 / beats.period, beats.strength, score)
            if best is None or score > best[0]:
                best = (score, item, path, beats)
        ok = best and not (kind in ("montage", "edit", "scene") and best[0] < float(self.m.min_beat_strength))
        if self.cleaner:  # остальные скачанные кандидаты больше не нужны
            self.cleaner.music_candidates(best[2] if ok else None, [p for p in paths if p])
        if not ok:
            return None
        _, item, path, beats = best
        label, credit = self._credit(item)
        self.state.mark_music_used(f"ov:{item['id']}")
        log.info("Выбран трек «%s» — %s (%s), %.0f BPM, старт с %.1f с",
                 item.get("title"), item.get("creator"), label, 60 / beats.period, beats.offset)
        return Track(path, item.get("title") or "", item.get("creator") or "", label,
                     item.get("foreign_landing_url") or "", credit, beats)

    # ---------- уже скачанные треки ----------
    def _from_cache(self, need: float, kind: str) -> Track | None:
        """Поиск онлайн не сработал — трек из скачанных раньше (music/auto: файл + описание с лицензией),
        под настроение эдита, с чётким битом; треки этого фильма не повторяем, использованные — в конце очереди."""
        queries = MOOD_QUERIES.get(self._mood, MOOD_QUERIES["tension"])
        pool = []
        for js in self.auto_dir.glob("*.json"):
            audio = next((a for a in (js.with_suffix(e) for e in MUSIC_EXT) if a.exists()), None)
            if not audio:
                continue
            try:
                item = json.loads(js.read_text(encoding="utf-8"))
            except Exception:
                continue
            dur = (item.get("duration") or 0) / 1000
            if f"ov:{item.get('id')}" in self.film_used or (dur and dur < max(need + 5, 50)):
                continue
            item["_fit"] = max(fit_score(item, q, True, self._mood) for q in queries)
            if item["_fit"] > 0:
                pool.append((item, audio))
        log.info("Музыка: среди скачанных подходят %d треков", len(pool))
        pool.sort(key=lambda x: (self.state.music_use_count(f"ov:{x[0]['id']}"), -x[0]["_fit"]))
        best = None
        for item, path in pool[: int(self.m.candidates)]:
            try:
                beats = analyze_track(path, need)
            except Exception as e:
                log.info("  не разобрал «%s»: %s", item.get("title"), e)
                continue
            used = self.state.music_use_count(f"ov:{item['id']}")
            score = (0.4 + self._score(beats, kind)) * item["_fit"] / (1 + 3 * used)
            if best is None or score > best[0]:
                best = (score, item, path, beats)
        if not best:
            return None
        _, item, path, beats = best
        label, credit = self._credit(item)
        self.state.mark_music_used(f"ov:{item['id']}")
        self.film_used.add(f"ov:{item['id']}")
        log.info("Выбран скачанный трек «%s» — %s (%s), %.0f BPM", item.get("title"), item.get("creator"), label,
                 60 / beats.period)
        return Track(path, item.get("title") or "", item.get("creator") or "", label,
                     item.get("foreign_landing_url") or "", credit, beats)

    # ---------- своя папка ----------
    def _from_folder(self, need: float) -> Track | None:
        tracks = sorted(p for p in self.dir.glob("*") if p.suffix.lower() in MUSIC_EXT)
        if not tracks:
            log.info("Своей музыки в папке music/ нет — эдит будет со звуком фильма")
            return None
        tracks.sort(key=lambda p: self.state.music_use_count(f"file:{p.name}"))
        path = tracks[0]
        self.state.mark_music_used(f"file:{path.name}")
        beats = analyze_track(path, need)
        log.info("Музыка из папки: %s, %.0f BPM", path.name, 60 / beats.period)
        return Track(path, path.stem, "", "", "", "", beats)
