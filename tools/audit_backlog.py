"""Read-only аудит очереди, исходников и точных дубликатов медиа."""
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(4*1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def main():
    out = ROOT / 'review' / 'backlog_audit'
    out.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect((ROOT/'data/state.sqlite3').as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    videos = [dict(r) for r in db.execute('select * from videos order by id')]
    db.close()
    pending = []
    for r in videos:
        if r['youtube_id']:
            continue
        p = Path(r['file'])
        source = ROOT / r['movie_path']
        r['file_exists'] = p.is_file()
        r['source_exists'] = source.is_file()
        r['ae_media_exists'] = (p.parent/'ae'/f'{p.stem}_media').is_dir()
        pending.append(r)
    (out/'pending.json').write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf-8')
    schedule = [r for r in videos if r['publish_at'] and r['youtube_id']
                and not r['youtube_id'].startswith(('replaced:', 'rejected'))]
    (out/'schedule_local.json').write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding='utf-8')
    bysize = {}
    for folder in ['output', 'reference', 'review']:
        for p in (ROOT/folder).rglob('*'):
            if p.is_file() and p.suffix.lower() in {'.mp4','.mov','.mkv','.m4v'}:
                bysize.setdefault(p.stat().st_size, []).append(p)
    dupes = []
    for size, paths in bysize.items():
        if len(paths) < 2:
            continue
        hashes = {}
        for p in paths:
            hashes.setdefault(digest(p), []).append(p)
        for h, same in hashes.items():
            if len(same) > 1:
                dupes.append(dict(size=size, sha256=h, paths=[str(p) for p in same]))
    (out/'duplicates.json').write_text(json.dumps(dupes, ensure_ascii=False, indent=2), encoding='utf-8')
    print('pending',len(pending),'files',sum(r['file_exists'] for r in pending),
          'missing_sources',sum(not r['source_exists'] for r in pending))
    print('duplicates',len(dupes),'recoverable_MiB',round(sum(d['size']*(len(d['paths'])-1) for d in dupes)/2**20,1))
    for d in dupes:
        print(round(d['size']/2**20,1),*[str(Path(p).relative_to(ROOT)) for p in d['paths']],sep=' | ')


if __name__ == '__main__':
    main()
