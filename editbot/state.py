"""SQLite-память агента: какие фильмы обработаны, что загружено, когда следующая публикация."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    path TEXT PRIMARY KEY,
    title TEXT,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_path TEXT,
    file TEXT,
    title TEXT,
    youtube_id TEXT,
    publish_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS music_used (
    id TEXT PRIMARY KEY,
    count INTEGER
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def processed_recently(self, movie_path: Path, days: int) -> bool:
        row = self.db.execute("SELECT processed_at FROM movies WHERE path = ?", (str(movie_path),)).fetchone()
        if not row or not row[0]:
            return False
        return _now() - datetime.fromisoformat(row[0]) < timedelta(days=days)

    def is_processed(self, movie_path: Path) -> bool:
        key = os.path.normcase(os.path.abspath(movie_path))
        rows = self.db.execute("SELECT path FROM movies WHERE processed_at IS NOT NULL").fetchall()
        return any(os.path.normcase(os.path.abspath(r[0])) == key for r in rows)

    def scheduled_ahead(self) -> int:
        """Сколько загруженных роликов ещё ждут своей публикации (запас в расписании)."""
        now = _now().isoformat()
        return self.db.execute("SELECT COUNT(*) FROM videos WHERE youtube_id IS NOT NULL AND publish_at > ?",
                               (now,)).fetchone()[0]

    def mark_processed(self, movie_path: Path, title: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO movies(path, title, processed_at) VALUES (?, ?, ?)",
            (str(movie_path), title, _now().isoformat()),
        )
        self.db.commit()

    def add_video(self, movie_path: Path, file: Path, title: str,
                  youtube_id: str | None, publish_at: datetime | None) -> None:
        # перемонтированный ролик заменяет старую невыложенную запись, чтобы не загрузить его дважды
        # (сравнение без учёта регистра — на Windows «Город» и «город» один и тот же файл)
        key = os.path.normcase(os.path.abspath(file))
        for row_id, other in self.db.execute("SELECT id, file FROM videos WHERE youtube_id IS NULL").fetchall():
            if os.path.normcase(os.path.abspath(other)) == key:
                self.db.execute("DELETE FROM videos WHERE id = ?", (row_id,))
        self.db.execute(
            "INSERT INTO videos(movie_path, file, title, youtube_id, publish_at, created_at) VALUES (?,?,?,?,?,?)",
            (str(movie_path), str(file), title, youtube_id,
             publish_at.isoformat() if publish_at else None, _now().isoformat()),
        )
        self.db.commit()

    def drop_pending(self, movie_path: Path) -> int:
        """Перед повторным монтажом фильма убираем его старые невыложенные ролики из очереди."""
        key = os.path.normcase(os.path.abspath(movie_path))
        rows = self.db.execute("SELECT id, movie_path FROM videos WHERE youtube_id IS NULL").fetchall()
        ids = [r[0] for r in rows if os.path.normcase(os.path.abspath(r[1])) == key]
        self.db.executemany("DELETE FROM videos WHERE id = ?", [(i,) for i in ids])
        self.db.commit()
        return len(ids)

    def pending_uploads(self) -> list[tuple[int, str, str]]:
        """Ролики, которые смонтированы, но не загружены (например, кончилась квота YouTube)."""
        return self.db.execute(
            "SELECT id, movie_path, file FROM videos WHERE youtube_id IS NULL ORDER BY id"
        ).fetchall()

    def set_uploaded(self, video_id: int, youtube_id: str, publish_at: datetime | None) -> None:
        self.db.execute(
            "UPDATE videos SET youtube_id = ?, publish_at = ? WHERE id = ?",
            (youtube_id, publish_at.isoformat() if publish_at else None, video_id),
        )
        self.db.commit()

    def music_use_count(self, track_id: str) -> int:
        row = self.db.execute("SELECT count FROM music_used WHERE id = ?", (track_id,)).fetchone()
        return row[0] if row else 0

    def mark_music_used(self, track_id: str) -> None:
        self.db.execute("INSERT INTO music_used(id, count) VALUES (?, 1) "
                        "ON CONFLICT(id) DO UPDATE SET count = count + 1", (track_id,))
        self.db.commit()

    def already_uploaded(self, title: str) -> str | None:
        """Ролик с таким же названием уже на канале — его YouTube ID (защита от дублей)."""
        norm = " ".join(title.lower().replace("ё", "е").split())
        for t, yt in self.db.execute("SELECT title, youtube_id FROM videos WHERE youtube_id IS NOT NULL").fetchall():
            if t and " ".join(t.lower().replace("ё", "е").split()) == norm:
                return yt
        return None

    def drop_video(self, video_id: int) -> None:
        self.db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        self.db.commit()

    def next_daily_slot(self, times: list[str]) -> datetime:
        """Ближайшее свободное время публикации из ежедневных слотов («09:00», «13:00», «17:00» по местному
        времени): не раньше чем через 20 минут и не ближе часа к уже запланированной публикации
        (свободные слоты между запланированными тоже заполняются)."""
        local_now = datetime.now().astimezone()
        after = _now() + timedelta(minutes=20)
        taken = [datetime.fromisoformat(r[0]) for r in self.db.execute(
            "SELECT publish_at FROM videos WHERE youtube_id IS NOT NULL AND publish_at IS NOT NULL").fetchall()]
        taken = [t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in taken]
        day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots = sorted(times)
        for d in range(0, 400):
            for t in slots:
                h, m = (int(x) for x in t.split(":"))
                cand = (day + timedelta(days=d)).replace(hour=h, minute=m).astimezone(timezone.utc)
                if cand >= after and all(abs((cand - x).total_seconds()) >= 3600 for x in taken):
                    return cand
        return after

    def set_publish_at(self, youtube_id: str, when: datetime) -> None:
        self.db.execute("UPDATE videos SET publish_at = ? WHERE youtube_id = ?", (when.isoformat(), youtube_id))
        self.db.commit()

    def next_publish_slot(self, interval_hours: float) -> datetime:
        row = self.db.execute("SELECT MAX(publish_at) FROM videos WHERE youtube_id IS NOT NULL").fetchone()
        earliest = _now() + timedelta(minutes=20)
        if row and row[0]:
            candidate = datetime.fromisoformat(row[0]) + timedelta(hours=interval_hours)
            return max(candidate, earliest)
        return earliest
