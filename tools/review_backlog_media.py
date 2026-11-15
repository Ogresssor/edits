"""Листы кадров очереди и проверка промежуточных копий AE без удаления."""
import html
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from editbot import ffmpeg


def stream_hash(p):
    proc = subprocess.run([ffmpeg.ffmpeg_exe(), '-v','error','-i',str(p),'-map','0:v:0',
                           '-map','0:a:0','-c','copy','-f','streamhash','-hash','sha256','-'],
                          capture_output=True, text=True, timeout=90)
    if proc.returncode:
        return None
    return proc.stdout.strip()


def main():
    out = ROOT/'review/backlog_audit'
    rows = json.loads((out/'pending.json').read_text(encoding='utf-8'))
    gallery = ['<!doctype html><meta charset="utf-8"><title>Очередь: кадры</title>',
               '<style>body{background:#161616;color:#eee;font:16px Arial;max-width:1100px;margin:30px auto}img{max-width:100%}section{margin:35px 0}a{color:#9cf}</style>',
               '<h1>Невыложенные ролики</h1><p>Листы кадров не заменяют просмотр со звуком.</p>']
    for row in rows:
        p = Path(row['file'])
        if not p.is_file():
            continue
        info = ffmpeg.probe(p)
        row['duration'] = info.duration
        row['dimensions'] = [info.width,info.height]
        sheet = out/f"video_{row['id']}.jpg"
        ffmpeg.run(['-i',str(p),'-vf',f'fps=8/{info.duration},scale=240:-2,tile=4x2',
                    '-frames:v','1',str(sheet)],timeout=120)
        gallery.append(f'<section><h2>{row["id"]}: {html.escape(row["title"])}</h2><p>{info.duration:.2f} с; '
                       f'исходник: {row["source_exists"]}; медиа AE: {row["ae_media_exists"]}</p>'
                       f'<img src="{sheet.name}"><p>{html.escape(str(p))}</p></section>')
        print('review',row['id'],info.duration,flush=True)
    (out/'gallery.html').write_text('\n'.join(gallery),encoding='utf-8')
    (out/'pending.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    cleanup = []
    for p in (ROOT/'output').rglob('render.mp4'):
        if p.parent.parent.name != 'ae' or not p.parent.name.endswith('_media'):
            continue
        target = p.parent.parent.parent/(p.parent.name[:-6]+'.mp4')
        if not target.is_file():
            continue
        h1,h2 = stream_hash(p),stream_hash(target)
        if h1 and h1 == h2:
            cleanup.append(dict(path=str(p.resolve()),keep=str(target.resolve()),size=p.stat().st_size,
                                reason='Промежуточный рендер AE; SHA-256 видео- и аудиопотоков совпадает с финалом'))
    (out/'cleanup_verified.json').write_text(json.dumps(cleanup,ensure_ascii=False,indent=2),encoding='utf-8')
    print('verified removable copies',len(cleanup),'GiB',round(sum(r['size'] for r in cleanup)/2**30,2),flush=True)


if __name__ == '__main__':
    main()
