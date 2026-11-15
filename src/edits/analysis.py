"""Full-duration technical scene map, explicitly distinct from editorial review."""
import re
from pathlib import Path
from . import media
from .core import read, save


def analyze(source: Path, store):
    source = source.resolve(); digest = store.source_hash(source)
    target = store.root / 'analysis' / (digest + '.json')
    if target.exists():
        return target
    info = media.probe(source)
    log = media.run(['-i', str(source), '-an', '-vf',
                     "scale=320:-2,select='gt(scene,0.35)',showinfo", '-f', 'null', '-'], timeout=14400)
    cuts = sorted(set(float(t) for t in re.findall(r'pts_time:([\d.]+)', log)))
    boundaries = [0.0] + [t for t in cuts if 0 < t < info['duration']] + [info['duration']]
    save(target, dict(source_sha256=digest, duration=info['duration'], method='full-duration FFmpeg scene detection',
                      complete_technical_scan=True, continuous_human_review=False, full_transcript=False,
                      scenes=[dict(start=a, end=b) for a,b in zip(boundaries,boundaries[1:])]))
    target.with_suffix('.log').write_text(log, encoding='utf-8')
    return target


def require_coverage(path: Path, digest: str, duration: float):
    data = read(path)
    if data.get('source_sha256') != digest or not data.get('complete_technical_scan'):
        raise ValueError('Complete source scene map required before editing')
    previous = 0.0
    for scene in data.get('scenes', []):
        if abs(scene['start'] - previous) > .02 or scene['end'] <= scene['start']:
            raise ValueError('Scene map contains a gap/overlap')
        previous = scene['end']
    if abs(previous - duration) > .15:
        raise ValueError('Scene map does not cover the complete source')
