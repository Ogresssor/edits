"""Оркестратор: прогоняет фильм через всех агентов и следит, чтобы ничего не делалось дважды."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .agents.analyzer import AnalyzerAgent
from .agents.curator import CuratorAgent
from .agents.editor import EditorAgent, safe_name
from .agents.metadata import MetadataAgent, VideoMeta
from .agents.music import MusicAgent
from .agents.research import ResearchAgent
from .agents.trends import Movie, TrendAgent, normalize
from .agents.uploader import QuotaExceeded, UploaderAgent
from .cleanup import Cleaner
from .config import Config
from . import subtitles
from .state import State

log = logging.getLogger("orchestrator")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")   # как publish_at в базе


def episode_tag(path: Path) -> str:
    """Номер серии из имени файла: s01e02 / 5x01 -> «s01e02» / «s05e01»; не сериал — пусто."""
    import re

    name = Path(path).stem.lower()
    m = re.search(r"s(\d{1,2})[ ._-]?e(\d{1,3})", name) or re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", name)
    if m:
        return f"s{int(m[1]):02d}e{int(m[2]):02d}"
    # «Season 1/01 Episode 1.m4v», «Сезон 2/Серия 3.mkv»: сезон — из папки, серия — из имени файла
    ep = re.search(r"(?:episode|серия|эпизод)[ ._-]?(\d{1,3})", name)
    season = re.search(r"(?:season|сезон)[ ._-]?(\d{1,2})", Path(path).parent.name.lower())
    return f"s{int(season[1]) if season else 1:02d}e{int(ep[1]):02d}" if ep else ""


VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts"}


class Orchestrator:
    def __init__(self, cfg: Config, upload: bool = True):
        self.cfg = cfg
        self.state = State(cfg.paths.path("state"))
        self.trends = TrendAgent(cfg, self.state)
        self.analyzer = AnalyzerAgent(cfg)
        self.research = ResearchAgent(cfg)
        self.curator = CuratorAgent(cfg)
        self.editor = EditorAgent(cfg)
        self.music = MusicAgent(cfg, self.state)
        self.cleaner = Cleaner(cfg, self.state)
        self.music.cleaner = self.cleaner
        self.metadata = MetadataAgent(cfg)
        self.uploader = UploaderAgent(cfg)
        self.upload_enabled = upload and bool(cfg.youtube.enabled)

    # ------------------------------------------------------------------
    def run_cycle(self, movie_path: Path | None = None, title: str | None = None, count: int | None = None,
                  fresh: bool = False, resume: bool = False) -> list[Path]:
        self.count, self.fresh, self.resume = count, fresh, resume
        if movie_path and fresh and (dropped := self.state.drop_pending(movie_path)):
            # фильм пересобирается с нуля (--fresh) — его прежние невыложенные версии не догружаем
            log.info("Прежние невыложенные эдиты этого фильма (%d) убраны из очереди загрузки", dropped)
        if self.upload_enabled:
            self.retry_pending()
        if movie_path:
            movie = Movie(movie_path, title or normalize(movie_path.stem).title())
        else:
            movie = self.trends.pick()
        if not movie:
            return []
        return self.process(movie)

    def process(self, movie: Movie) -> list[Path]:
        started = time.time()
        an = self.analyzer.analyze(movie)
        if not an.scenes and len(an.moments) < 4:
            log.error("Не нашлось ярких сцен в «%s» — пропускаю", movie.title)
            self.state.mark_processed(movie.path, movie.title)
            return []
        # исследователь-«режиссёр»: справка из Википедии/Викицитатника/TMDB и задания остальным агентам
        brief = self.research.brief(movie, an.workdir, an.info.duration, an.story)
        if brief.title and movie.title == normalize(movie.path.stem).title():
            # название из имени файла («The Fast And The Furious Tokyo Drift») -> русское из справки
            movie.extra.setdefault("original_title", brief.original_title)
            movie.title, movie.year = brief.title, movie.year or str(brief.year or "")
        subtitles.set_vocabulary(brief.names)            # субтитрам — имена героев
        subtitles.set_film(self.cfg, movie.title)        # и исправления, которые нужны только в этом фильме
        self.music.hints = brief.music_queries           # музыке — жанр фильма
        self.music.film_used = set()                     # в эдитах одного фильма треки не повторяются
        from . import looks
        look = looks.choose(str(self.cfg.edit.get("look", "auto")), brief.genres, brief.keywords)
        log.info("Образ эдитов: %s (жанр: %s)", look.name, ", ".join(brief.genres) or "—")
        if not self.music.hints:
            self.music.hints = look.music
        style = str(self.cfg.edit.get("style", "arc"))
        count = int(getattr(self, "count", None) or self.cfg.curation.get("edits_per_movie", 3))
        if style == "arc":      # сюжетная линия: реплики -> разгон -> дроп и быстрая нарезка (как у эталонных каналов)
            plans = self.curator.curate_arcs(movie, an, brief, count, getattr(self, "fresh", False))
        elif style == "scene":
            plans = self.curator.curate_scenes(movie, an, brief, count, getattr(self, "fresh", False))
        else:   # прежние форматы: эдит из фрагментов разных мест / мини-истории
            plans = self.curator.curate(movie, an, brief)
        for plan in plans:
            plan.look = look
        fresh = getattr(self, "fresh", False)
        if (fresh or getattr(self, "redo", False)) and (dropped := self.state.drop_pending(movie.path)):
            log.info("Старые невыложенные эдиты этого фильма (%d) убраны из очереди загрузки", dropped)

        # каждый фильм — своя папка: output/<Название фильма>/
        out_dir = self.cfg.paths.path("output") / safe_name(movie.title)
        ep = episode_tag(movie.path)
        if ep:                                           # сериал: у каждой серии своя папка (s01e02, 5x01…)
            out_dir = out_dir / ep
        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []
        used_tracks: list[Path] = []
        n_story = 0
        for i, plan in enumerate(plans, 1):
            if plan.kind == "arc":
                n_story += 1
                out_file = out_dir / f"{n_story}_{safe_name(plan.theme)}.mp4"
                need = 60.0
            elif plan.kind == "scene":
                n_story += 1
                out_file = out_dir / f"{n_story}_{safe_name(plan.theme)}.mp4"
                need = sum(e - s for s, e in plan.segments) + 14
            elif plan.kind in ("story", "edit"):
                n_story += 1
                out_file = out_dir / f"{n_story}_{safe_name(plan.theme)}.mp4"
                need = (sum(f.end - f.start for f in plan.fragments) * 1.3 + 6 if plan.fragments
                        else sum(e - s for s, e in plan.segments) + 6)
            else:
                out_file = out_dir / f"общий_эдит_{safe_name(plan.theme)}.mp4"
                need = float(self.cfg.edit.montage_duration)
            if getattr(self, "redo", False):
                # --redo: те же сцены в новом формате; отклонённые и уже вышедшие не трогаем, отложенные заменяем
                row = self._made_row(movie.path, plan.theme)
                if row and (row[1] == "rejected" or (row[1] and row[2] and row[2] <= _utcnow_iso())):
                    log.info("Ролик %d «%s» отклонён или уже вышел — не переделываю", i, plan.theme)
                    continue
                if row and row[1]:
                    self.state.db.execute("UPDATE videos SET youtube_id = ? WHERE id = ?",
                                          (f"replaced:{row[1]}", row[0]))
                    self.state.db.commit()
                    log.info("Ролик %d «%s» был в отложке (%s) — переделываю, старую версию уберите из расписания",
                             i, plan.theme, row[1])
            elif not fresh and (done := self._made_before(movie.path, plan.theme)):
                # повторный запуск (ещё роликов, продолжение прерванного): готовые и выложенные не переделываем
                log.info("Ролик %d «%s» уже смонтирован (%s) — пропускаю", i, plan.theme, Path(done).name)
                results.append(Path(done))
                continue
            if getattr(self, "resume", False) and out_file.exists() and out_file.with_suffix(".json").exists():
                # прерванный запуск успел записать ролик, но не базу — возвращаем его в очередь
                title = json.loads(out_file.with_suffix(".json").read_text(encoding="utf-8"))["title"]
                log.info("Ролик %d уже готов (%s) — пропускаю", i, out_file.name)
                self.state.add_video(movie.path, out_file, title, None, None)
                results.append(out_file)
                continue
            try:
                track = self.music.pick(plan, need)
                video = self._render_checked(movie, an, plan, i, out_file, track)
                if track:
                    used_tracks.append(track.path)
            except Exception:
                log.exception("Не удалось смонтировать ролик %d «%s»", i, plan.theme)
                continue
            meta = self.metadata.generate(movie, plan, track.credit if track else "", brief)
            video.with_suffix(".json").write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
            log.info("Название: %s", meta.title)
            self.state.add_video(movie.path, video, meta.title, None, None)
            results.append(video)

        self.state.mark_processed(movie.path, movie.title)
        # уборка: музыка уже вшита в ролики, старые версии роликов и временные файлы монтажа не нужны
        self.cleaner.music_used(used_tracks)
        self.cleaner.stale_videos()
        self.cleaner.render_temp(an.workdir)
        log.info("Фильм «%s»: готово %d эдитов за %.1f мин", movie.title, len(results), (time.time() - started) / 60)
        if self.upload_enabled:
            self.retry_pending()
        return results

    def batch(self, folder: Path, title: str | None, count: int | None, keep_ahead: int, limit: int) -> int:
        """Серии по порядку: пропускает уже смонтированные; монтирует следующую, пока в расписании YouTube
        меньше keep_ahead роликов, ждущих публикации (и не больше limit серий за запуск). Возвращает число серий."""
        files = sorted(p for p in Path(folder).rglob("*") if p.suffix.lower() in VIDEO_EXT)
        done = 0
        if self.upload_enabled:
            self.retry_pending()                          # сначала догружаем очередь (если вчера кончилась квота)
        for f in files:
            if done >= limit:
                break
            if self.state.is_processed(f):
                continue
            ahead = self.state.scheduled_ahead() + len(self.state.pending_uploads())
            if self.upload_enabled and ahead >= keep_ahead:
                log.info("В расписании уже %d роликов наперёд (нужно %d) — следующую серию смонтирую позже",
                         ahead, keep_ahead)
                break
            log.info("=== Серия %s (%s)", episode_tag(f) or f.stem, f.name)
            self.run_cycle(f, title, count=count)
            done += 1
        left = sum(1 for f in files if not self.state.is_processed(f))
        log.info("Серий смонтировано за запуск: %d, осталось: %d", done, left)
        return done

    def _vpn_ok(self) -> bool:
        """Загрузка — только через VPN пользователя (youtube.vpn: страна и сеть). Проверяется перед каждым роликом:
        VPN может смениться посреди загрузки. Не удалось проверить — тоже не грузим."""
        want = self.cfg.youtube.get("vpn") or {}
        if not want:
            return True
        try:
            import requests
            ip = requests.get("https://ipinfo.io/json", timeout=10).json()
        except Exception as e:
            log.warning("Не проверить, через какой VPN идёт соединение (%s) — загрузку не начинаю", e)
            return False
        bad = [f"{k}: {ip.get(k, '?')} вместо {v}" for k, v in (("country", want.get("country")), ("org", want.get("org")))
               if v and not str(ip.get(k, "")).startswith(str(v))]
        if bad:
            log.warning("Соединение не через ваш VPN (%s) — загрузку не начинаю. Включите VPN США и повторите",
                        "; ".join(bad))
            return False
        return True

    def _made_before(self, movie_path: Path, theme: str) -> str | None:
        """Ролик этой сюжетной линии фильма уже есть в базе (в очереди или выложен) — путь к нему.
        Номер в имени файла не важен: при повторном отборе линии меняют порядок."""
        name, key = safe_name(theme), os.path.normcase(os.path.abspath(movie_path))
        for mp, f in self.state.db.execute("SELECT movie_path, file FROM videos").fetchall():
            if os.path.normcase(os.path.abspath(mp)) == key and Path(f).stem.split("_", 1)[-1] == name:
                return f
        return None

    def _made_row(self, movie_path: Path, theme: str) -> tuple[int, str | None, str | None] | None:
        """Как _made_before, но запись базы целиком: (id, youtube_id, publish_at)."""
        name, key = safe_name(theme), os.path.normcase(os.path.abspath(movie_path))
        for vid, mp, f, yt, at in self.state.db.execute(
                "SELECT id, movie_path, file, youtube_id, publish_at FROM videos ORDER BY id DESC").fetchall():
            if os.path.normcase(os.path.abspath(mp)) == key and Path(f).stem.split("_", 1)[-1] == name:
                return vid, yt, at
        return None

    def _render_checked(self, movie: Movie, an, plan, i: int, out_file: Path, track):
        """Рендер с проверкой результата и одной повторной попыткой в безопасном режиме
        (без нейросетевого увеличения) — чтобы один сбой (память видеокарты, битый кусок) не терял ролик."""
        from . import ffmpeg

        last_error: Exception | None = None
        for attempt in (1, 2):
            self.editor.safe_mode = attempt == 2
            try:
                video = self.editor.render(movie, an, plan, i, out_file, track)
                info = ffmpeg.probe(video)
                if info.duration < 5 or not info.has_audio or video.stat().st_size < 200_000:
                    raise RuntimeError(f"ролик не прошёл проверку: {info.duration:.1f} с, "
                                       f"звук={info.has_audio}, {video.stat().st_size} байт")
                return video
            except Exception as e:
                last_error = e
                log.warning("Попытка %d для «%s» не удалась: %s", attempt, plan.theme, e)
            finally:
                self.editor.safe_mode = False
        raise RuntimeError(f"не удалось смонтировать «{plan.theme}»: {last_error}")

    def _publish_time(self) -> datetime | None:
        y = self.cfg.youtube
        if y.schedule and y.privacy == "public":
            times = list(y.get("publish_times") or [])
            if times:                                    # ежедневные слоты (13:00, 19:00 …)
                return self.state.next_daily_slot([str(t) for t in times])
            return self.state.next_publish_slot(float(y.interval_hours))
        return None

    def retry_pending(self, only: str | None = None) -> None:
        """Загружает все смонтированные, но ещё не выложенные ролики (only — только те, чей путь содержит текст)."""
        hold = [str(h).lower() for h in (self.cfg.youtube.get("hold") or [])]
        for video_id, _movie, file in self.state.pending_uploads():
            if not self._vpn_ok():
                return
            path = Path(file)
            if only and only.lower() not in str(path).lower():
                continue
            if not only and any(h in str(path).lower() for h in hold):
                continue                                 # придержано (youtube.hold) — грузим только явно через --only
            meta_file = path.with_suffix(".json")
            if not path.exists() or not meta_file.exists():
                continue
            meta = VideoMeta(**json.loads(meta_file.read_text(encoding="utf-8")))
            dup = self.state.already_uploaded(meta.title)
            if dup:                                      # такой ролик уже на канале — второй раз не грузим
                log.info("«%s» уже на канале (https://youtu.be/%s) — пропускаю, дубль не загружаю", meta.title, dup)
                self.state.drop_video(video_id)
                continue
            publish_at = self._publish_time()
            try:
                yt_id = self.uploader.upload(path, meta, publish_at)
            except QuotaExceeded as e:
                log.warning("%s — оставшиеся ролики загружу в следующем цикле", e)
                return
            except Exception:
                log.exception("Ошибка загрузки %s", path.name)
                return
            self.state.set_uploaded(video_id, yt_id, publish_at or datetime.now(timezone.utc))
            self.cleaner.after_upload(path)
            media = path.parent / "ae" / f"{path.stem}_media"
            if self.cfg._data.get("cleanup", {}).get("ae_media_after_upload", True) and media.is_dir():
                import shutil
                shutil.rmtree(media, ignore_errors=True)    # куски для After Effects больше не нужны (~250 МБ)
                log.info("Удалил рабочие куски After Effects для «%s» (ролик и проект остаются)", path.stem)

    # ------------------------------------------------------------------
    def daemon(self) -> None:
        hours = float(self.cfg.scheduler.cycle_hours)
        log.info("Автономный режим: новый фильм каждые %.1f ч. Остановка — Ctrl+C", hours)
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Цикл завершился с ошибкой, продолжу по расписанию")
            log.info("Следующий цикл через %.1f ч", hours)
            time.sleep(hours * 3600)
