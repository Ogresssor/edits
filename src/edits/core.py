from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def read(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def validate_plan(plan):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', plan['id']):
        raise ValueError('id: only ASCII letters, digits, underscore and hyphen')
    for key in ('hook', 'development', 'payoff', 'context', 'selection_reason'):
        if not str(plan.get('story', {}).get(key, '')).strip():
            raise ValueError('Missing editorial field: ' + key)
    if plan.get('rating') not in ('strong', 'medium', 'weak'):
        raise ValueError('Specify strong/medium/weak rating')
    segments = plan.get('segments', [])
    if not segments:
        raise ValueError('No segments')
    previous = -1
    for segment in segments:
        start, end = segment['start'], segment['end']
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) for t in (start, end)):
            raise ValueError('Invalid segment timestamps')
        if start < 0 or end <= start or start < previous:
            raise ValueError('Segments must be positive, chronological and non-overlapping')
        previous = end
    duration = sum(s['end'] - s['start'] for s in segments)
    previous = 0
    for caption in plan.get('captions', []):
        start, end = caption['start'], caption['end']
        if not (math.isfinite(start) and math.isfinite(end) and previous <= start < end <= duration + 0.001):
            raise ValueError('Caption timestamps must be ordered, non-overlapping, inside the edit')
        text = caption['text']
        if not text.strip() or any(char in text for char in ('{', '}', '\\', '\n', '\r')):
            raise ValueError('Caption must contain plain text without ASS commands')
        if len(text) > 100:
            raise ValueError('Caption too long; split into readable phrases')
        previous = end
    if plan.get('music'):
        if not all(plan['music'].get(k) for k in ('path', 'creator', 'title', 'license_url', 'source_url')):
            raise ValueError('Music requires provenance and license')
    return duration


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS artifacts (
                    sha TEXT PRIMARY KEY, path TEXT NOT NULL, story TEXT NOT NULL,
                    revision TEXT NOT NULL, qa TEXT NOT NULL, approved_by TEXT, approved_at TEXT);
                CREATE TABLE IF NOT EXISTS queue (
                    sha TEXT PRIMARY KEY REFERENCES artifacts(sha), story TEXT UNIQUE NOT NULL,
                    publish_at TEXT UNIQUE NOT NULL, state TEXT NOT NULL DEFAULT 'planned',
                    youtube_id TEXT UNIQUE);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / 'studio.sqlite', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, file: Path, story: str, revision: str, qa: dict):
        digest = sha256(file)
        if qa.get('sha256') != digest or not qa.get('passed'):
            raise ValueError('Artifact did not pass QA')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO artifacts(sha,path,story,revision,qa) VALUES(?,?,?,?,?)',
                       (digest, str(file.resolve()), story, revision, json.dumps(qa)))
        return digest

    def approve(self, digest: str, reviewer: str, watched: bool):
        if not watched or not reviewer.strip():
            raise ValueError('Complete video/audio review and reviewer identity required')
        with self.connect() as db:
            row = db.execute('SELECT * FROM artifacts WHERE sha=?', (digest,)).fetchone()
            if not row or sha256(Path(row['path'])) != digest:
                raise ValueError('Missing or changed artifact')
            db.execute('UPDATE artifacts SET approved_by=?,approved_at=? WHERE sha=?',
                       (reviewer, datetime.now(timezone.utc).isoformat(), digest))

    def enqueue(self, digest: str, when: datetime):
        if when.tzinfo is None or when <= datetime.now(timezone.utc):
            raise ValueError('Future timezone-aware publication time required')
        msk = when.astimezone(timezone(timedelta(hours=3)))
        if (msk.hour not in (9, 13, 17)) or msk.minute or msk.second or msk.microsecond:
            raise ValueError('Allowed slots: 09:00, 13:00, 17:00 Moscow')
        with self.connect() as db:
            row = db.execute('SELECT * FROM artifacts WHERE sha=?', (digest,)).fetchone()
            if not row or not row['approved_by'] or sha256(Path(row['path'])) != digest:
                raise ValueError('Only the exact approved export may be queued')
            db.execute('INSERT INTO queue(sha,story,publish_at) VALUES(?,?,?)',
                       (digest, row['story'], when.astimezone(timezone.utc).isoformat()))

    def queue(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM queue ORDER BY publish_at')]

    def source_hash(self, path: Path):
        """Full hash on ingest/change; reuse on unchanged local file stats."""
        path = path.resolve(); stat = path.stat()
        signature = dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)
        cache = self.root / 'source-cache' / (canonical(str(path)) + '.json')
        if cache.exists():
            previous = read(cache)
            if previous.get('signature') == signature:
                return previous['sha256']
        digest = sha256(path)
        save(cache, dict(signature=signature, sha256=digest))
        return digest
