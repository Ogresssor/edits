"""Уборщик: следит, чтобы фабрика эдитов не съедала диск.

Что удаляется (каждый пункт включается в config.yaml -> cleanup):
  * music_candidates   — скачанные, но не выбранные треки (из 8 кандидатов в ролик идёт один);
  * music_after_render — и выбранные треки после монтажа: музыка уже вшита в ролик, а счётчик
                         использований (чтобы не повторяться) хранится в базе;
  * stale_videos       — ролики в output/, которых нет в базе: старые версии после перемонтажа,
                         переименованные темы, файлы прошлого формата в корне output/;
  * render_temp        — временные файлы монтажа work/<фильм>/render_* и папки без анализа;
  * after_upload       — сам ролик после успешной загрузки на YouTube (описание .json остаётся).

Кэш анализа фильма (сигналы, расшифровка, карта сюжета) не трогается — без него повторный запуск
снова потратит ~15 минут на расшифровку.

Ручной запуск: `python main.py clean` (с `--dry-run` — только показать, что будет удалено).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .config import Config
from .state import State

log = logging.getLogger("cleanup")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
MUSIC_EXT = {".mp3", ".ogg", ".oga", ".wav", ".flac", ".m4a", ".opus"}


def _key(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


class Cleaner:
    def __init__(self, cfg: Config, state: State, dry_run: bool = False):
        self.cfg = cfg
        self.state = state
        self.c = cfg.get("cleanup", None)
        self.dry = dry_run
        self.freed = 0

    def on(self, key: str, default: bool = True) -> bool:
        if self.c is None:
            return default
        return bool(self.c.get("enabled", True)) and bool(self.c.get(key, default))

    def _remove(self, p: Path, why: str) -> None:
        if not p.exists():
            return
        size = _size(p)
        log.info("%s %s (%s, %.1f МБ)", "Удалил бы" if self.dry else "Удаляю", p, why, size / 2 ** 20)
        if not self.dry:
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError as e:
                log.warning("  не удалось удалить %s: %s", p, e)
                return
        self.freed += size

    # ---------------------------------------------------------------- музыка
    def music_candidates(self, chosen: Path | None, downloaded: list[Path]) -> None:
        """Сразу после выбора трека — удаляем остальных скачанных кандидатов."""
        if not self.on("music_candidates"):
            return
        keep = _key(chosen) if chosen else None
        for p in downloaded:
            if p and _key(p) != keep and self._is_auto(p):
                self._remove(p, "кандидат, не выбран")
                self._remove(p.with_suffix(".json"), "описание кандидата")

    def music_used(self, tracks: list[Path]) -> None:
        """После монтажа фильма — удаляем и выбранные треки (музыка уже в роликах)."""
        if not self.on("music_after_render"):
            return
        for p in tracks:
            if p and self._is_auto(p):
                self._remove(p, "уже вшит в ролик")
                self._remove(p.with_suffix(".json"), "описание трека")

    def _is_auto(self, p: Path) -> bool:
        # трогаем только автоскачанную музыку, свою папку music/ — никогда
        auto = self.cfg.paths.path("music") / "auto"
        return _key(p.parent) == _key(auto)

    def music_cache(self) -> None:
        """Ручная уборка: весь кэш автоскачанной музыки."""
        if not (self.on("music_candidates") or self.on("music_after_render")):
            return
        auto = self.cfg.paths.path("music") / "auto"
        for p in sorted(auto.glob("*")) if auto.exists() else []:
            if p.is_file() and (p.suffix.lower() in MUSIC_EXT or p.suffix.lower() in {".json", ".part"}):
                self._remove(p, "кэш скачанной музыки")

    # ---------------------------------------------------------------- ролики
    def stale_videos(self) -> None:
        """Ролики в output/, которых нет в базе (старые версии, прошлый формат)."""
        if not self.on("stale_videos"):
            return
        known = {_key(f) for (f,) in self.state.db.execute("SELECT file FROM videos").fetchall()}
        out = self.cfg.paths.path("output")
        for p in sorted(out.rglob("*")) if out.exists() else []:
            if "ae" in p.relative_to(out).parts[:-1]:
                continue                     # куски проектов After Effects (.aep ссылается на них) — не трогаем
            if p.is_file() and p.suffix.lower() in VIDEO_EXT and _key(p) not in known:
                self._remove(p, "старая версия — нет в базе")
                self._remove(p.with_suffix(".json"), "описание старой версии")
        for d in sorted(out.iterdir(), reverse=True) if out.exists() else []:
            if d.is_dir() and not any(d.iterdir()) and not self.dry:
                d.rmdir()

    def after_upload(self, video: Path) -> None:
        if self.on("after_upload", False):
            self._remove(video, "загружен на YouTube")

    # ---------------------------------------------------------------- временные файлы
    def render_temp(self, workdir: Path | None = None) -> None:
        if not self.on("render_temp"):
            return
        work = self.cfg.paths.path("work")
        dirs = [workdir] if workdir else [d for d in work.iterdir() if d.is_dir()] if work.exists() else []
        for d in dirs:
            if workdir is None and not (d / "analysis.json").exists() and d.name != "fonts":
                self._remove(d, "временная папка без анализа фильма")
                continue
            for r in sorted(d.glob("render_*")):
                self._remove(r, "временные файлы монтажа")
            for f in list(d.glob("speech_*.wav")) + list(d.glob("full_audio.wav")) + list(d.glob("lang_*.wav")):
                self._remove(f, "временный звук")

    # ---------------------------------------------------------------- всё сразу
    def all(self) -> None:
        self.music_cache()
        self.stale_videos()
        self.render_temp()
        log.info("Уборка: %s %.1f МБ", "можно освободить" if self.dry else "освобождено", self.freed / 2 ** 20)
