"""Whole-film navigation and local review excerpts; no YouTube access."""
import argparse
import json
import re
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from editbot import ffmpeg

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'review/spiderman_full_review'
SOURCE=ROOT/'library/Spider.Man.2002.OM.WEB-DL.AMZN.1080p-SOFCJ.mkv'
CHAPTERS=[(0,388.472,'Знакомство с Питером'),(388.472,895.187,'Лаборатория, укус'),
 (895.187,1312.604,'Опыт Нормана, перемены Питера'),(1312.604,1517.767,'Столовая и Флэш'),
 (1517.767,2074.115,'Паутина, крыши, новая цель'),(2074.115,2421.170,'Дядя Бен и подготовка к бою'),
 (2421.170,2592.883,'Реслинг и организатор'),(2592.883,3271.978,'Потеря Бена и начало героя'),
 (3271.978,3647.937,'Спасения и работа фотографом'),(3647.937,3859.774,'Фотографии и редактор'),
 (3859.774,4331.370,'Праздник, Гоблин и спасение'),(4331.370,4774.521,'Норман и Гоблин'),
 (4774.521,5112.608,'Переулок и поцелуй'),(5112.608,5975.679,'Ужин и раскрытие тайны'),
 (5975.679,6307.385,'Мост и выбор'),(6307.385,7271.904,'Финал, похороны и титры')]


def tc(t):
    t=int(t);return f'{t//3600:02}:{t//60%60:02}:{t%60:02}'


def subtitles():
    text=(OUT/'film_ru.srt').read_text(encoding='utf-8-sig')
    rows=[]
    for block in re.split(r'\r?\n\s*\r?\n',text.strip()):
        m=re.search(r'(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})',block)
        if not m:continue
        v=list(map(int,m.groups()))
        a=v[0]*3600+v[1]*60+v[2]+v[3]/1000
        b=v[4]*3600+v[5]*60+v[6]+v[7]/1000
        caption=re.sub('<[^>]+>','',block[m.end():]).strip().replace('\n',' / ')
        rows.append(dict(start=a,end=b,text=caption))
    (OUT/'subtitles.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    return rows


def navigate():
    from PIL import Image,ImageDraw
    rows=subtitles()
    for i,(a,b,title) in enumerate(CHAPTERS,1):
        folder=OUT/'chapters';folder.mkdir(exist_ok=True)
        notes='\n'.join(f"{tc(r['start'])} {r['text']}" for r in rows if a<=r['start']<b)
        (folder/f'{i:02}.txt').write_text(notes,encoding='utf-8')
        dest=folder/f'{i:02}.jpg'
        if not dest.exists():
            ffmpeg.run(['-ss',str(a),'-i',str(SOURCE),'-t',str(b-a),'-vf',
                f'fps=36/{b-a},scale=320:180,tile=6x6','-frames:v','1',str(dest)],timeout=180)
            im=Image.open(dest);draw=ImageDraw.Draw(im)
            for j in range(36):
                x=(j%6)*320;y=(j//6)*180
                draw.rectangle((x,y,x+115,y+18),fill='black')
                draw.text((x+3,y+2),'~'+tc(a+(j+.5)*(b-a)/36),fill='white')
            im.save(dest,quality=86)
        print('CHAPTER',i,tc(a),tc(b),title,len(notes),flush=True)


def excerpts():
    data=json.loads((OUT/'candidates.json').read_text(encoding='utf-8'))
    folder=OUT/'previews';folder.mkdir(exist_ok=True)
    for c in data:
        dest=folder/(c['id']+'.mp4')
        if dest.exists():continue
        ffmpeg.run(['-ss',str(c['start']),'-i',str(SOURCE),'-t',str(c['end']-c['start']),
            '-map','0:v:0','-map','0:a:0','-vf','scale=854:-2',
            '-c:v','libx264','-preset','veryfast','-crf','27','-c:a','aac','-b:a','96k','-ac','2',
            '-movflags','+faststart',str(dest)],timeout=180)
        print('PREVIEW',c['id'],flush=True)


def verify():
    from html.parser import HTMLParser
    data=json.loads((OUT/'candidates.json').read_text(encoding='utf-8'))
    results=[]
    for c in data:
        dest=OUT/'previews'/(c['id']+'.mp4')
        info=ffmpeg.probe(dest)
        assert info.has_audio and info.width==854
        assert abs(info.duration-(c['end']-c['start']))<.25,(c['id'],info.duration)
        ffmpeg.run(['-i',str(dest),'-f','null','-'],timeout=90)
        results.append(dict(id=c['id'],duration=info.duration,has_audio=info.has_audio,full_decode=True,bytes=dest.stat().st_size))
    for i in range(1,17):
        assert (OUT/'chapters'/f'{i:02}.jpg').exists()
        assert (OUT/'chapters'/f'{i:02}.txt').exists()
    class Links(HTMLParser):
        def handle_starttag(self,tag,attrs):
            for k,v in attrs:
                if k in {'href','src'} and not v.startswith(('http','#','data:')):
                    assert (OUT/v).is_file(),v
    Links().feed((OUT/'index.html').read_text(encoding='utf-8'))
    (OUT/'verification.json').write_text(json.dumps(dict(chapters=16,frames_reviewed=576,
        previews=results,local_links_valid=True),ensure_ascii=False,indent=2),encoding='utf-8')
    print('VERIFIED',len(results),'previews, 16 chapter sheets, catalog links',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['navigate','excerpts','verify'])
    args=parser.parse_args();OUT.mkdir(exist_ok=True)
    if args.action=='navigate':navigate()
    elif args.action=='excerpts':excerpts()
    else:verify()
