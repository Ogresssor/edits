"""One local Spider-Man story, native motion converted to 24 fps; no upload."""
import argparse
import json
import logging
import math
import subprocess
import sys
from pathlib import Path
import series_batch as batch
import remaining_season as checks

ROOT=batch.ROOT
OUT=ROOT/'review/spiderman_2002_first'
KEY='flash_first_fight'
SOURCE=ROOT/'library/Spider.Man.2002.OM.WEB-DL.AMZN.1080p-SOFCJ.mkv'
FPS=24
TITLE='Флэш ещё не знал, с кем связался'
PLATE=['ФЛЭШ ЕЩЁ НЕ ЗНАЛ,','С КЕМ СВЯЗАЛСЯ']
# Chronological, complete phrases. Wide frame on the physical confrontation.
PARTS=[(1434.70,1447.60,1440),(1455.00,1463.40,1440),
       (1465.30,1494.90,1920),(1495.35,1502.80,1440)]


def select():
    batch.OUT=OUT
    checks.OUT=OUT
    checks.select=lambda key: None


def prepare(model=None):
    import cv2
    folder=OUT/KEY
    media=folder/'media'
    media.mkdir(parents=True,exist_ok=True)
    model=model or batch.whisper()
    clips=[]
    events=[]
    transcript=[]
    offset=0
    for i,(a,b,width) in enumerate(PARTS):
        frames=round((b-a)*FPS)
        duration=frames/FPS
        file=media/f'part_{i:02}.mov'
        recipe=dict(source=str(SOURCE),a=a,b=b,width=width,frames=frames,fps=FPS,audio='0:a:0',edge_fade_ms=8)
        receipt=file.with_suffix('.recipe.json')
        if not (file.exists() and receipt.exists() and json.loads(receipt.read_text(encoding='utf-8'))==recipe):
            height=round(1080*1080/width/2)*2
            vf=f'crop={width}:1080:{(1920-width)//2}:0,scale=1080:{height}:flags=lanczos,pad=1080:1200:0:{(1200-height)//2}:black,setsar=1,fps=24'
            batch.ffmpeg.run(['-ss',str(a),'-i',str(SOURCE),'-t',str(duration),
                '-map','0:v:0','-map','0:a:0','-vf',vf,'-frames:v',str(frames),
                '-c:v','libx264','-crf','16','-preset','slow','-pix_fmt','yuv420p',
                '-af',f'aresample=48000,afade=t=in:d=0.008,afade=t=out:st={duration-.008}:d=0.008',
                '-ac','2','-c:a','pcm_s16le',str(file)],timeout=240)
            batch.save(receipt,recipe)
        cap=cv2.VideoCapture(str(file)); actual=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));cap.release()
        assert abs(actual-frames)<=1,(actual,frames)
        segments,_=model.transcribe(str(file),language='ru',word_timestamps=True,
            vad_filter=False,beam_size=5,condition_on_previous_text=False)
        rows=[dict(start=s.start,end=s.end,text=s.text,
            words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segments]
        # Speechless action sometimes triggers Whisper's generic credit hallucinations.
        rows=[r for r in rows if not any(t in r['text'].lower() for t in ['продолжение следует','субтитр','спасибо за просмотр'])]
        words=batch.fixed_words(rows)
        for w in words:
            w.text=w.text.replace('Хэрри','Гарри')
        words=[w for w in words if 0<=w.start<w.end<=duration+.03]
        for e in batch.captions(words,duration):
            e['start']+=offset;e['end']=min(e['end']+offset,offset+duration)
            events.append(e)
        transcript.extend(dict(start=r['start']+offset,text=r['text'],
            words=[[w[0]+offset,w[1]+offset,w[2]] for w in r['words']]) for r in rows)
        clips.append(dict(file=file.resolve().as_posix(),start=offset,dur=duration,
            last_frame=(actual-1)/FPS,voice=True,fx={},name=f'{a:.2f}–{b:.2f}'))
        offset=round(offset+duration,6)
        print('PREPARED',i,duration,flush=True)
    data=dict(name=TITLE,w=1080,h=1200,fps=FPS,total=offset,
        clips=clips,subs=events,flashes=[],music=None,
        plate=dict(lines=PLATE,font='Montserrat-SemiBold',size=39,dur=4),
        sub_style=dict(top_font='Montserrat-SemiBold',main_font='Montserrat-ExtraBoldItalic',
            top_size=42,main_size=76,y_pct=62,top_tracking=120,main_tracking=20,
            glow_threshold=90,glow_radius=28,glow_intensity=1.3))
    batch.save(folder/'timeline.json',data)
    batch.save(folder/'clean_fragment_transcript.json',transcript)
    batch.save(folder/'plan.json',dict(source=str(SOURCE),parts=PARTS,audio='Russian theatrical dub, first audio stream',added_music=False))
    concat=media/'concat.txt'
    concat.write_text('\n'.join(f"file 'part_{i:02}.mov'" for i in range(len(PARTS))),encoding='utf-8')
    batch.ffmpeg.run(['-f','concat','-safe','0','-i',str(concat),'-c:v','copy','-c:a','aac','-b:a','192k',
        '-movflags','+faststart',str(folder/'clean_cut.mp4')],timeout=120)
    batch.ffmpeg.run(['-i',str(folder/'clean_cut.mp4'),'-vf',f'fps=24/{offset},scale=270:-2,tile=4x6',
        '-frames:v','1',str(folder/'framing_sheet.jpg')],timeout=120)
    print('READY',offset,flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,stream=sys.stdout)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','render','check'])
    args=parser.parse_args();select()
    if args.action=='prepare':prepare()
    elif args.action=='render':batch.render(KEY)
    else:checks.check_final(KEY,batch.whisper())
