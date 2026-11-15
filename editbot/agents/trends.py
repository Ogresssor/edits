"""Агент трендов: берёт популярные сейчас фильмы из TMDB и сопоставляет их с вашей библиотекой."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import requests

from ..config import Config, env
from ..state import State

log = logging.getLogger("trends")

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv"}

# Мусор из имён файлов релизов: качество, кодеки, источники, языки.
_JUNK = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr10?|dv|sdr|bluray|blu ray|bdrip|brrip|web ?dl|webrip|web|hdrip|"
    r"dvdrip|hdtv|remux|x264|x265|h ?264|h ?265|hevc|avc|aac\d?|ac3|dts|ddp?\d?|atmos|10bit|8bit|"
    r"rus|eng|ukr|dub|mvo|avo|lostfilm|extended|imax|proper|repack|hd)\b"
)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[._\-\[\]\(\)]+", " ", text)
    text = re.sub(r"\b(19|20)\d{2}\b.*$", "", text)  # всё после года обычно теги релиза
    text = _JUNK.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Movie:
    path: Path
    title: str                     # человеческое название (из TMDB или из имени файла)
    year: str = ""
    overview: str = ""
    popularity: float = 0.0
    trend_rank: int | None = None
    tmdb_id: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class TrendingMovie:
    tmdb_id: int
    title: str
    original_title: str
    year: str
    overview: str
    popularity: float
    rank: int


def scan_library(library: Path) -> list[Path]:
    files = [p for p in library.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    # сэмплы и трейлеры внутри папок релизов нам не нужны
    return [p for p in files if not re.search(r"\b(sample|trailer)\b", p.stem.lower())]


def _library_key(path: Path, library: Path) -> list[str]:
    """Варианты названия файла: имя файла и имя папки (часто название фильма — это папка)."""
    keys = [normalize(path.stem)]
    if path.parent != library:
        keys.append(normalize(path.parent.name))
    return [k for k in keys if k]


class TrendAgent:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.library = cfg.paths.path("library")

    # ---------- TMDB ----------
    def fetch_trending(self) -> list[TrendingMovie]:
        key = env("TMDB_API_KEY")
        if not key or not self.cfg.trends.enabled:
            log.warning("TMDB_API_KEY не задан — тренды недоступны, беру фильмы из библиотеки по порядку")
            return []
        headers, params = {"accept": "application/json"}, {"language": self.cfg.trends.language}
        if key.startswith("eyJ"):  # v4 Read Access Token (JWT)
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key

        result: list[TrendingMovie] = []
        seen: set[int] = set()
        endpoints = ["trending/movie/week", "trending/movie/day", "movie/now_playing", "movie/popular"]
        for endpoint in endpoints:
            for page in range(1, int(self.cfg.trends.pages) + 1):
                try:
                    r = requests.get(f"https://api.themoviedb.org/3/{endpoint}", headers=headers,
                                     params={**params, "page": page}, timeout=20)
                    r.raise_for_status()
                except requests.RequestException as e:
                    log.warning("TMDB %s стр.%d: %s", endpoint, page, e)
                    break
                for item in r.json().get("results", []):
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    result.append(TrendingMovie(
                        tmdb_id=item["id"],
                        title=item.get("title") or item.get("original_title", ""),
                        original_title=item.get("original_title", ""),
                        year=(item.get("release_date") or "")[:4],
                        overview=item.get("overview", ""),
                        popularity=float(item.get("popularity", 0)),
                        rank=len(result) + 1,
                    ))
        log.info("TMDB: получено %d популярных фильмов", len(result))
        return result

    # ---------- сопоставление ----------
    @staticmethod
    def _similarity(file_keys: list[str], movie: TrendingMovie) -> float:
        best = 0.0
        for title in {normalize(movie.title), normalize(movie.original_title)}:
            if not title:
                continue
            for key in file_keys:
                score = SequenceMatcher(None, key, title).ratio()
                # файл «oppenheimer imax edition» vs «oppenheimer» — засчитываем совпадение по префиксу
                if min(len(key), len(title)) >= 5 and (key.startswith(title) or title.startswith(key)):
                    score = max(score, 0.9)
                best = max(best, score)
        return best

    def match(self, files: list[Path], trending: list[TrendingMovie]) -> list[Movie]:
        movies: list[Movie] = []
        threshold = float(self.cfg.trends.min_match)
        for path in files:
            keys = _library_key(path, self.library)
            best, best_score = None, 0.0
            for t in trending:
                s = self._similarity(keys, t)
                if s > best_score:
                    best, best_score = t, s
            if best and best_score >= threshold:
                movies.append(Movie(path, best.title, best.year, best.overview, best.popularity,
                                    best.rank, best.tmdb_id, {"original_title": best.original_title}))
            else:
                pretty = (path.parent.name if path.parent != self.library else path.stem)
                movies.append(Movie(path, normalize(pretty).title() or path.stem))
        return movies

    def pick(self) -> Movie | None:
        files = scan_library(self.library)
        if not files:
            log.error("Библиотека пуста: положите фильмы в %s", self.library)
            return None
        trending = self.fetch_trending()
        movies = self.match(files, trending)
        days = int(self.cfg.trends.reprocess_after_days)
        fresh = [m for m in movies if not self.state.processed_recently(m.path, days)]
        if not fresh:
            log.info("Все фильмы библиотеки уже обработаны за последние %d дн.", days)
            return None
        # Сначала самые трендовые, затем остальные (новые файлы — первыми).
        fresh.sort(key=lambda m: (m.trend_rank is None, m.trend_rank or 0, -m.path.stat().st_mtime))
        chosen = fresh[0]
        if chosen.trend_rank:
            log.info("Выбран трендовый фильм: «%s» (%s), место в трендах #%d",
                     chosen.title, chosen.year, chosen.trend_rank)
        else:
            log.info("Трендовых фильмов в библиотеке нет, беру «%s»", chosen.title)
        self._log_missing(trending, movies)
        return chosen

    def _log_missing(self, trending: list[TrendingMovie], movies: list[Movie]) -> None:
        have = {m.tmdb_id for m in movies if m.tmdb_id}
        missing = [t for t in trending[:15] if t.tmdb_id not in have]
        if missing:
            log.info("Топ трендов, которых нет в библиотеке: %s",
                     "; ".join(f"{t.title} ({t.year})" for t in missing[:10]))

    def report(self) -> list[tuple[TrendingMovie, Path | None]]:
        """Для команды `trends`: тренды и найденные у вас файлы."""
        files = scan_library(self.library)
        trending = self.fetch_trending()
        matched = {m.tmdb_id: m.path for m in self.match(files, trending) if m.tmdb_id}
        return [(t, matched.get(t.tmdb_id)) for t in trending]
