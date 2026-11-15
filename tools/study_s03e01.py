"""Расшифровка первой серии нового сезона для ручного смыслового отбора."""
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import torch
    lib = Path(torch.__file__).parent/'lib'
    os.environ['PATH'] = str(lib)+os.pathsep+os.environ.get('PATH','')
    dll = os.add_dll_directory(str(lib))
    from editbot.config import load_config
    from editbot.agents.analyzer import movie_workdir
    from editbot import subtitles, ffmpeg
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', stream=sys.stdout)
    cfg = load_config()
    source = next((ROOT/'library/Peaky.Blinders.S03.1080p.BluRay').glob('*S03E01*.mkv'))
    work = movie_workdir(cfg,source)
    work.mkdir(parents=True,exist_ok=True)
    info = ffmpeg.probe(source)
    # AlexFilm — первая явно размеченная русская дорожка этого релиза.
    index = info.audio_titles.index('AlexFilm 5.1')
    out = ROOT/'review/s03e01'
    out.mkdir(parents=True,exist_ok=True)
    (out/'source.json').write_text(json.dumps(dict(source=str(source),work=str(work),
        duration=info.duration,audio_index=index,audio_titles=info.audio_titles),ensure_ascii=False,indent=2),encoding='utf-8')
    print('WORK',work,flush=True)
    words = subtitles.film_words(cfg,source,index,work)
    from editbot.arcs import sentences
    lines = sentences(words)
    (out/'dialogue.txt').write_text('\n'.join(f'{a:8.2f}–{b:8.2f}  {t}' for a,b,t in lines),encoding='utf-8')
    print('DONE',len(words),'words',len(lines),'sentences',flush=True)


if __name__=='__main__':
    main()
