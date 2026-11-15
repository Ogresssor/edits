"""Небольшая публичная выборка референсов без cookies и авторизации."""
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reference' / 'channel_sample_2026-11-02'
HANDLES = ['Maven-Edits', 'Sanaamovie', 'Abyssal5', 'Kevedits', 'peter3edits', 'Fyzenix_Editz']


def collect(handle):
    cmd = [sys.executable, '-m', 'yt_dlp', '--flat-playlist', '--playlist-end', '12',
           '--dump-json', '--skip-download', '--socket-timeout', '15', '--retries', '0',
           f'https://www.youtube.com/@{handle}/shorts']
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=90)
    entries = [json.loads(line) for line in p.stdout.splitlines() if line.startswith('{')]
    clean = [{k: x.get(k) for k in ('id', 'title', 'view_count', 'url')} for x in entries]
    (OUT / f'{handle}.json').write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
    print(handle, len(clean), 'entries', flush=True)
    if not clean:
        print(p.stderr[-500:], flush=True)
    return handle, clean


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        samples = list(pool.map(collect, HANDLES))
    # По одному ролику каждого канала: самый просматриваемый из 12 последних,
    # а не «самый популярный за всё время».
    for handle, entries in samples:
        if not entries:
            continue
        item = max(entries, key=lambda x: x.get('view_count') or 0)
        dest = OUT / handle
        dest.mkdir(exist_ok=True)
        p = subprocess.run([sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-overwrites',
                            '--js-runtimes', 'node', '--ffmpeg-location', imageio_ffmpeg.get_ffmpeg_exe(),
                            '--write-info-json', '--socket-timeout', '15', '--retries', '0',
                            '-f', 'bv[width<=720][ext=mp4]+ba[ext=m4a]/b[width<=720]',
                            '--merge-output-format', 'mp4',
                            '-o', str(dest / '%(id)s.%(ext)s'), item['url']],
                           capture_output=True, text=True, encoding='utf-8', timeout=120)
        print(handle, item['id'], 'download', p.returncode, p.stderr[-700:], flush=True)
