"""Агент-исследователь («режиссёр»): собирает сведения о фильме из авторитетных источников и раздаёт
задания остальным агентам.

Источники (все — открытые API, без парсинга чужих страниц):
  * Википедия (ru + en): сюжет, актёры и персонажи, жанр;
  * Викицитатник (ru + en): известные цитаты — по ним куратор находит культовые сцены;
  * TMDB (если задан TMDB_API_KEY): описание, слоган, жанры, ключевые слова, персонажи.

Из этого получается справка (Brief) и задания:
  * куратору — культовые сцены и цитаты, которые стоит найти, персонажи, граница спойлеров;
  * субтитрам — словарь имён (Whisper перестаёт писать «Дакаши» вместо «Такаши»);
  * музыке — запросы под жанр фильма;
  * метаданным — оригинальное название, актёры, хэштеги.
С ANTHROPIC_API_KEY задания пишет Claude, прочитав справку и карту сюжета; без ключа — правила.
Результат кэшируется в work/<фильм>/research.json (его можно поправить вручную).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import anthropic
import requests

from ..config import Config, env
from .trends import Movie

log = logging.getLogger("research")

RESEARCH_VERSION = 1
UA = {"User-Agent": "editbot/1.0 (personal movie-edit tool; python-requests) research-agent"}

# жанр (ru/en) -> запросы музыки (Openverse) под настроение фильма
GENRE_MUSIC = [
    (("гонк", "racing", "street racing", "drift", "дрифт"), ["hip hop beat", "electronic drive", "phonk"]),
    (("боевик", "action"), ["action", "dark epic", "trailer"]),
    (("комеди", "comedy"), ["funny", "upbeat"]),
    (("ужас", "horror"), ["dark ambient", "horror"]),
    (("триллер", "thriller"), ["suspense", "dark tension"]),
    (("драм", "drama"), ["emotional piano", "sad"]),
    (("фантаст", "science fiction", "sci-fi"), ["epic synth", "cinematic"]),
    (("фэнтези", "fantasy", "сказк"), ["epic adventure", "fantasy orchestral"]),
    (("мультфильм", "анимац", "animation"), ["adventure", "playful"]),
    (("мелодрам", "romance"), ["romantic", "emotional"]),
]


@dataclass
class Brief:
    title: str
    original_title: str = ""
    year: str = ""
    plot: str = ""                       # сюжет по-русски (Википедия / TMDB)
    plot_en: str = ""
    tagline: str = ""
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    cast: list[list[str]] = field(default_factory=list)      # [[актёр, персонаж], ...]
    names: list[str] = field(default_factory=list)           # имена персонажей (словарь для субтитров)
    quotes: list[str] = field(default_factory=list)          # известные цитаты
    sources: list[str] = field(default_factory=list)
    # задания агентам
    iconic: list[str] = field(default_factory=list)          # что стоит найти куратору
    spoiler_after: float = 0.0                               # с этой секунды фильма — спойлеры (не брать)
    music_queries: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)

    def for_curator(self) -> str:
        """Справка для куратора (текстом в запрос)."""
        from .. import ffmpeg

        out = [f"СПРАВКА О ФИЛЬМЕ (источники: {', '.join(self.sources) or '—'})",
               f"«{self.title}» / {self.original_title or '—'} ({self.year or '—'}); жанр: "
               f"{', '.join(self.genres) or '—'}"]
        if self.tagline:
            out.append(f"Слоган: {self.tagline}")
        if self.cast:
            out.append("Персонажи: " + "; ".join(f"{c} ({a})" for a, c in self.cast[:14]))
        if self.plot:
            out.append("Сюжет: " + self.plot[:3500])
        elif self.plot_en:
            out.append("Plot: " + self.plot_en[:3500])
        if self.quotes:
            out.append("Известные цитаты (найди эти сцены в карте — культовые моменты):\n"
                       + "\n".join(f"- {q}" for q in self.quotes[:25]))
        if self.iconic:
            out.append("Задание: найти и рассмотреть в первую очередь:\n" + "\n".join(f"- {x}" for x in self.iconic))
        if self.spoiler_after:
            out.append(f"Спойлеры: всё после {ffmpeg.fmt_time(self.spoiler_after)} не брать (финал фильма).")
        return "\n".join(out)


def _get(url: str, params: dict, timeout: int = 20) -> dict:
    import time

    for attempt in range(4):
        r = requests.get(url, params=params, headers=UA, timeout=timeout)
        if r.status_code == 429 and attempt < 3:  # Википедия просит притормозить
            try:
                wait = float(r.headers.get("Retry-After", 0))
            except ValueError:
                wait = 0
            time.sleep(min(15.0, max(wait, 3.0 * (attempt + 1))))
            continue
        r.raise_for_status()
        return r.json()
    return {}


# ---------------------------------------------------------------- Википедия / Викицитатник
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s.lower().replace("ё", "е"))).strip()


def _wiki_search(lang: str, title: str, year: str) -> str | None:
    """Статья именно об этом фильме: похожее название + год + слово «фильм» в описании
    (а не соседняя часть франшизы или актёр)."""
    from difflib import SequenceMatcher

    film = "фильм" if lang == "ru" else "film"
    data = _get(f"https://{lang}.wikipedia.org/w/api.php",
                {"action": "query", "list": "search", "srsearch": f"{title} {year} {film}".strip(),
                 "srlimit": 10, "format": "json"})
    hits = data.get("query", {}).get("search", [])
    want = _norm(title)

    def score(h: dict) -> float:
        name = _norm(re.sub(r"\(.*?\)", "", h["title"]))
        snippet = h.get("snippet", "").lower()
        s = SequenceMatcher(None, want, name).ratio()
        s += 0.25 if (year and year in snippet + h["title"]) else 0
        s += 0.1 if film in snippet + h["title"].lower() else 0
        s += 0.15 if f"({film}" in h["title"].lower() else 0
        if re.findall(r"\d+", want) != re.findall(r"\d+", name):     # «Форсаж 5» — не «Форсаж (фильм, 2001)»
            s -= 0.5
        if re.search(r"саундтрек|soundtrack|альбом|album|игра|video game|франшиз|franchise|роман|novel",
                     h["title"].lower()):
            s -= 1.0
        return s

    best = max(hits, key=score, default=None)
    return best["title"] if best and score(best) >= 0.6 else None


def _wiki_page(site: str, title: str) -> tuple[str, dict[str, str]]:
    """Текст статьи без разметки и межъязыковые ссылки."""
    data = _get(f"https://{site}/w/api.php", {
        "action": "query", "prop": "extracts|langlinks", "explaintext": 1, "exsectionformat": "wiki",
        "titles": title, "redirects": 1, "lllimit": 50, "format": "json"})
    page = next(iter(data.get("query", {}).get("pages", {}).values()), {})
    links = {ll["lang"]: ll["*"] for ll in page.get("langlinks", [])}
    return page.get("extract", "") or "", links


def _sections(text: str) -> dict[str, str]:
    """«== Заголовок ==» -> текст раздела (вложенные подразделы входят в родителя)."""
    out: dict[str, str] = {}
    cur = "_intro"
    for line in text.splitlines():
        m = re.match(r"^(=+)\s*(.+?)\s*=+$", line.strip())
        if m:
            if len(m.group(1)) == 2:
                cur = m.group(2).lower()
            continue
        out[cur] = out.get(cur, "") + line + "\n"
    return {k: v.strip() for k, v in out.items()}


def _find(sections: dict[str, str], *keys: str) -> str:
    for k, v in sections.items():
        if any(key in k for key in keys):
            return v
    return ""


def _parse_cast(text: str) -> list[list[str]]:
    """Строки вида «Лукас Блэк — Шон Босуэлл» / «Lucas Black as Sean Boswell»."""
    cast = []
    for line in text.splitlines():
        line = line.strip(" •*-\t")
        m = re.match(r"^([^—–:(]{3,40}?)\s+(?:—|–|-|as)\s+(.{2,})$", line)
        if m:
            actor, role = m.group(1).strip(), re.split(r"[,;(:]|\s[—–-]\s", m.group(2))[0].strip(" .")
            if role and len(role.split()) <= 5:
                cast.append([actor, role])
    return cast[:25]


def _cast_from_plot(plot: str) -> list[list[str]]:
    """«Шон Босуэлл (Лукас Блэк)» в тексте сюжета -> [актёр, персонаж]."""
    cast: list[list[str]] = []
    for role, actor in re.findall(r"([А-ЯЁ][а-яё]+(?:[ -][А-ЯЁ][а-яё]+)?)\s+\(([А-ЯЁ][^()]{3,40}?)\)", plot):
        if not any(role == r for _, r in cast):
            cast.append([actor.strip(), role])
    return cast[:25]


def _quotes(site: str, title: str) -> list[str]:
    try:
        text, _ = _wiki_page(site, title)
    except Exception:
        return []
    quotes = []
    for line in text.splitlines():
        line = line.strip()
        # реплики в Викицитатнике: «Персонаж: текст» или просто текст цитаты
        line = re.sub(r"^[A-ZА-ЯЁ][\w .'-]{1,25}:\s+", "", line)
        line = re.sub(r"\s*\[[^\]]*\]\s*", " ", line).strip()   # ремарки [толпа завывает]
        if 15 <= len(line) <= 220 and not line.startswith(("==", "Источник", "Source", "See also", "External")):
            if not re.search(r"(Directed by|Written by|Taglines?|Cast|http|ISBN|imdb|IMDb|Режисс|сценари|В ролях)",
                             line):
                quotes.append(line)
    return quotes[:40]


# ---------------------------------------------------------------- TMDB
def _tmdb(title: str, year: str, key: str) -> dict:
    base, p = "https://api.themoviedb.org/3", {"api_key": key, "language": "ru-RU"}
    found = _get(f"{base}/search/movie", {**p, "query": title, **({"year": year} if year else {})})
    if not found.get("results"):
        return {}
    mid = found["results"][0]["id"]
    d = _get(f"{base}/movie/{mid}", {**p, "append_to_response": "credits,keywords"})
    return d


# ---------------------------------------------------------------- агент
DIRECTOR_SYSTEM = """Ты — режиссёр-постановщик канала с короткими эдитами по фильмам. Тебе дают справку о фильме
из открытых источников (Википедия, Викицитатник, TMDB) и карту сюжета (весь фильм блоками по 10 секунд с репликами).
Раздай задания команде:
- iconic: 6–12 конкретных сцен, которые стоит сделать роликами (культовые реплики, гонки, драки, смешные
  и трогательные моменты), каждая — «что происходит + примерное время по карте»;
- spoiler_after: время в формате «1:23:45», после которого начинаются финал и главные повороты (не брать);
- names: имена персонажей и важные слова так, как их пишут в русском дубляже (для распознавания речи);
- music_queries: 3–5 английских запросов для поиска музыки в духе фильма;
- hashtags: 5–8 хэштегов (фильм, актёры, жанр)."""

DIRECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "iconic": {"type": "array", "items": {"type": "string"}},
        "spoiler_after": {"type": "string"},
        "names": {"type": "array", "items": {"type": "string"}},
        "music_queries": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["iconic", "spoiler_after", "names", "music_queries", "hashtags"],
    "additionalProperties": False,
}


class ResearchAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.r = cfg.get("research", None)
        self.client = (anthropic.Anthropic()
                       if (cfg.curation.use_claude and env("ANTHROPIC_API_KEY")) else None)

    def _opt(self, key: str, default):
        return self.r.get(key, default) if self.r is not None else default

    def brief(self, movie: Movie, workdir: Path, duration: float, story: Path | None = None) -> Brief:
        cache = workdir / "research.json"
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.pop("version", None) == RESEARCH_VERSION:
                    log.info("Справка о фильме взята из %s", cache.name)
                    return Brief(**data)
            except Exception as e:
                log.warning("Не удалось прочитать %s (%s), собираю заново", cache.name, e)

        b = Brief(title=movie.title, original_title=str(movie.extra.get("original_title") or ""),
                  year=str(movie.year or ""), plot=movie.overview or "")
        if self._opt("enabled", True):
            self._collect(b)
        self._assign(b, duration, story)
        cache.write_text(json.dumps({"version": RESEARCH_VERSION, **asdict(b)}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        log.info("Справка: %s; персонажей %d, цитат %d, заданий куратору %d",
                 ", ".join(b.sources) or "источники недоступны", len(b.cast), len(b.quotes), len(b.iconic))
        return b

    # ---------- сбор ----------
    def _collect(self, b: Brief) -> None:
        latin = bool(re.search(r"[A-Za-z]", b.title)) and not re.search(r"[А-Яа-яЁё]", b.title)
        ru_title = en_title = None
        try:
            if latin:
                en_title = _wiki_search("en", b.title, b.year)
            else:
                ru_title = _wiki_search("ru", b.title, b.year)
        except Exception as e:
            log.warning("Википедия: поиск не удался (%s)", e)

        ru_text = en_text = ""
        try:
            if ru_title:
                ru_text, links = _wiki_page("ru.wikipedia.org", ru_title)
                en_title = links.get("en")
            if en_title:
                en_text, links = _wiki_page("en.wikipedia.org", en_title)
                if not ru_title and links.get("ru"):
                    ru_title = links["ru"]
                    ru_text, _ = _wiki_page("ru.wikipedia.org", ru_title)
        except Exception as e:
            log.warning("Википедия: статья не загрузилась (%s)", e)

        if ru_text:
            sec = _sections(ru_text)
            b.sources.append(f"ru.wikipedia.org/wiki/{ru_title.replace(' ', '_')}")
            b.plot = _find(sec, "сюжет") or b.plot or sec.get("_intro", "")
            b.cast = _parse_cast(_find(sec, "в ролях", "актёр", "роли")) or _cast_from_plot(b.plot)
            m = re.search(r"\b(19|20)\d\d\b", sec.get("_intro", ""))
            b.year = b.year or (m.group(0) if m else "")
            intro = sec.get("_intro", "").lower()
            b.genres = [g for g in ("боевик", "триллер", "комедия", "драма", "фантастика", "фэнтези", "ужасы",
                                    "мелодрама", "мультфильм", "приключения", "криминал", "гонки")
                        if g[:-1] in intro]
        if en_text:
            sec = _sections(en_text)
            b.sources.append(f"en.wikipedia.org/wiki/{en_title.replace(' ', '_')}")
            b.plot_en = _find(sec, "plot", "synopsis")
            b.original_title = b.original_title or en_title.split(" (")[0]
            if not b.cast:
                b.cast = _parse_cast(_find(sec, "cast"))
            intro = sec.get("_intro", "").lower()
            b.keywords += [k for k in ("street racing", "drift", "heist", "martial arts", "superhero", "racing")
                           if k in intro]
        # Викицитатник: сначала русский, затем английский
        for site, t in (("ru.wikiquote.org", ru_title), ("en.wikiquote.org", en_title)):
            if t and not b.quotes:
                q = _quotes(site, t.split(" (")[0]) or _quotes(site, t)
                if q:
                    b.quotes = q
                    b.sources.append(f"{site}/wiki/{t.replace(' ', '_')}")
        # TMDB
        key = env("TMDB_API_KEY")
        if key:
            try:
                d = _tmdb(b.title, b.year, key)
                if d:
                    b.sources.append(f"themoviedb.org/movie/{d['id']}")
                    b.tagline = d.get("tagline") or ""
                    b.original_title = b.original_title or d.get("original_title", "")
                    b.genres = b.genres or [g["name"].lower() for g in d.get("genres", [])]
                    b.keywords += [k["name"] for k in d.get("keywords", {}).get("keywords", [])][:15]
                    if not b.cast:
                        b.cast = [[c["name"], c.get("character", "")] for c in d.get("credits", {}).get("cast", [])
                                  if c.get("character")][:20]
                    b.plot = b.plot or d.get("overview", "")
            except Exception as e:
                log.warning("TMDB: не удалось получить данные (%s)", e)

    # ---------- задания ----------
    def _assign(self, b: Brief, duration: float, story: Path | None) -> None:
        # имена персонажей -> словарь для Whisper (по-русски: первое слово роли — обычно имя)
        names = []
        for _, role in b.cast:
            for w in re.findall(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?", role):
                # в тексте сюжета имена бывают в творительном падеже: «с Клэем», «с Нилой»
                w = re.sub(r"(?<=\w{3})ем$", "й", w)
                w = re.sub(r"(?<=\w{3})ой$", "а", w)
                if w not in names:
                    names.append(w)
        b.names = names[:20]
        tail = float(self._opt("spoiler_tail_pct", 18)) / 100
        b.spoiler_after = round(duration * (1 - tail), 1) if duration else 0.0
        hay = " ".join([*b.genres, *b.keywords, b.plot_en[:600].lower(), b.plot[:600].lower()])
        queries: list[str] = []
        for keys, q in GENRE_MUSIC:
            if any(k in hay for k in keys):
                queries += [x for x in q if x not in queries]
        b.music_queries = queries[:5] or ["cinematic", "epic"]
        tag = lambda s: "#" + re.sub(r"\W+", "", s.title())
        b.hashtags = [tag(b.title)] + ([tag(b.original_title)] if b.original_title else []) + \
                     [tag(a) for a, _ in b.cast[:3]]
        b.iconic = [f"сцена с цитатой: «{q}»" for q in b.quotes[:8]]

        if self.client and story and story.exists():
            try:
                self._director(b, story)
            except Exception as e:
                log.warning("Режиссёр Claude недоступен (%s) — задания по правилам", e)

    def _director(self, b: Brief, story: Path) -> None:
        from .curator import parse_time

        r = self.client.beta.messages.create(
            model=self.cfg.curation.model,
            max_tokens=8000,
            system=DIRECTOR_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": b.for_curator()},
                {"type": "text", "text": story.read_text(encoding="utf-8")},
                {"type": "text", "text": "Раздай задания строго по схеме."},
            ]}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": DIRECTOR_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if r.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"stop_reason={r.stop_reason}")
        d = json.loads(next(x.text for x in r.content if x.type == "text"))
        b.iconic = d["iconic"] or b.iconic
        b.spoiler_after = parse_time(d["spoiler_after"]) or b.spoiler_after
        b.names = list(dict.fromkeys([*d["names"], *b.names]))[:30]
        b.music_queries = d["music_queries"] or b.music_queries
        b.hashtags = list(dict.fromkeys([*b.hashtags, *d["hashtags"]]))[:10]
        log.info("Режиссёр Claude раздал задания: %d сцен", len(b.iconic))
