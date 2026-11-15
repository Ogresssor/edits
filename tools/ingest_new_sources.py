"""Inventory complete new sources, extract full text subtitles and navigation sheets."""
import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from editbot import ffmpeg

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'review/new_sources_2026-11-11'


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def parse_srt(path):
    rows=[]
    for block in re.split(r'\r?\n\s*\r?\n',path.read_text(encoding='utf-8-sig').strip()):
        m=re.search(r'(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})',block)
        if not m:continue
        v=list(map(int,m.groups()));start=v[0]*3600+v[1]*60+v[2]+v[3]/1000
        end=v[4]*3600+v[5]*60+v[6]+v[7]/1000
        text=re.sub(r'<[^>]*>|\{[^}]*\}','',block[m.end():]).strip().replace('\n',' / ')
        rows.append(dict(start=start,end=end,text=text))
    return rows


def main(action):
    OUT.mkdir(exist_ok=True)
    files=sorted((ROOT/'library').rglob('*.mkv'))
    assert len(files)==7,'Expected one film and six episodes'
    items=[]
    for source in files:
        ep=re.search(r's04e(\d\d)',source.name,re.I)
        key='peaky_s04e'+ep[1] if ep else 'spiderman2'
        folder=OUT/key;folder.mkdir(exist_ok=True)
        if action=='inventory':
            run=subprocess.run([ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(source)],
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            (folder/'probe.txt').write_text(run.stderr,encoding='utf-8')
            info=ffmpeg.probe(source)
            subs=[]
            for index,lang,codec in re.findall(r'Stream #0:(\d+)(?:\[[^]]+\])?(?:\(([^)]+)\))?: Subtitle: ([^ ,\r\n]+)',run.stderr):
                if codec not in {'subrip','ass','ssa','webvtt','mov_text','text'}:continue
                path=folder/f'sub_{index}_{lang or "und"}.srt'
                ffmpeg.run(['-i',str(source),'-map',f'0:{index}','-c:s','srt',str(path)],timeout=90)
                rows=parse_srt(path);save(path.with_suffix('.json'),rows)
                subs.append(dict(index=int(index),language=lang,codec=codec,rows=len(rows),
                                 characters=sum(len(r['text']) for r in rows),file=path.name))
            full=[s for s in subs if s['rows']>100]
            russian=[s for s in full if s['language'] in {'rus','ru'}]
            chosen=max(russian or full,key=lambda s:s['characters'],default=None)
            if chosen:
                rows=parse_srt(folder/chosen['file']);save(folder/'subtitles.json',rows)
                text='\n'.join(f"{r['start']:.2f}–{r['end']:.2f} {r['text']}" for r in rows)
                (folder/'dialogue.txt').write_text(text,encoding='utf-8')
            item=dict(key=key,source=str(source.resolve()),bytes=source.stat().st_size,
                      info=asdict(info),subtitles=subs,chosen_subtitle=chosen)
            save(folder/'source.json',item)
            print(key,json.dumps(item,ensure_ascii=False),flush=True)
        else:
            item=json.loads((folder/'source.json').read_text(encoding='utf-8'))
            sheets=folder/'coverage';sheets.mkdir(exist_ok=True)
            if not (sheets/'coverage.json').exists():
                ffmpeg.run(['-skip_frame','nokey','-i',str(source),'-an','-sn',
                    '-vf','fps=1/20,scale=256:144,tile=6x6','-q:v','4',str(sheets/'sheet_%02d.jpg')],timeout=600)
                from PIL import Image,ImageDraw
                paths=sorted(sheets.glob('sheet_*.jpg'))
                for i,path in enumerate(paths):
                    image=Image.open(path);draw=ImageDraw.Draw(image)
                    for j in range(36):
                        t=(i*36+j)*20+10
                        if t>=item['info']['duration']:continue
                        x=j%6*256;y=j//6*144
                        draw.rectangle((x,y,x+100,y+18),fill='black')
                        draw.text((x+3,y+2),'~'+ffmpeg.fmt_time(t),fill='white')
                    image.save(path,quality=86)
                save(sheets/'coverage.json',dict(source_duration=item['info']['duration'],
                    interval_seconds=20,method='I-frames sampled over complete source; labels approximate',
                    sheets=[p.name for p in paths],full_source_demux_completed=True))
            print('COVERAGE',key,len(list(sheets.glob('sheet_*.jpg'))),flush=True)
        items.append(item)
    if action=='inventory':save(OUT/'sources.json',items)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    p=argparse.ArgumentParser();p.add_argument('action',choices=['inventory','coverage']);args=p.parse_args()
    main(args.action)
