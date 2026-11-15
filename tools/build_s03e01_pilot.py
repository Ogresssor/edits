"""Один смысловой пилот из S03E01 с прежними субтитрами AE.

--prepare: чистые фрагменты, музыка, план и превью без AE.
--render-ae: собрать подготовленный проект прежним шаблоном After Effects.
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot import ffmpeg, reframe, subtitles
from editbot.config import load_config

OUT=ROOT/'review/s03e01'
MEDIA=OUT/'media'
W,H,FPS=1080,1200,25
SEGMENTS=[(541.44,560.80,'Свадьба и родственники Грейс'),
          (599.32,625.60,'Запрет драк и немедленное нарушение')]
CAPTION_RANGES=[(541.44,544.72),(544.72,546.96),(546.96,549.48),
    (549.48,552.10),(552.10,553.83),(553.83,557.20),(557.20,560.80),
    (599.32,602.90),(603.10,605.25),(605.25,608.10),(609.00,612.95),
    (612.95,614.50),(614.90,616.20),(617.30,620.44),(620.44,621.40),
    (622.80,624.40)]


def prepare():
    import cv2
    cfg=load_config()
    MEDIA.mkdir(parents=True,exist_ok=True)
    source=next((ROOT/'library/Peaky.Blinders.S03.1080p.BluRay').glob('*S03E01*.mkv'))
    cache=json.loads((ROOT/'work/fbba97a50cd0/words.json').read_text(encoding='utf-8'))
    words=subtitles._join_hyphens([subtitles.Word(a,b,t) for a,b,t in cache['words']])
    clips, mapped, events=[],[],[]
    offset=0.0
    for i,(start,end,name) in enumerate(SEGMENTS):
        dur=round(end-start,2)
        # Только малые кадры для поиска лиц/склеек; чистый мастер кодируется из оригинала.
        proc=subprocess.run([ffmpeg.ffmpeg_exe(),'-v','error','-ss',str(start),'-i',str(source),
                             '-t',str(dur),'-vf','scale=480:260','-pix_fmt','bgr24','-f','rawvideo','-'],
                            capture_output=True,timeout=120)
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode('utf-8','replace')[-1000:])
        frames=np.frombuffer(proc.stdout,np.uint8).reshape(-1,260,480,3)
        centers,cuts=reframe.analyze_frames(frames,FPS)
        cropw=1100
        path=reframe.plan_path(centers,cuts,FPS,cropw/1920/2,'smart')
        xs=np.clip(path*1920-cropw/2,0,1920-cropw)
        command=MEDIA/f'framing_{i:02}.txt'
        command.write_text('\n'.join(f'{k/FPS:.3f} crop@framing x {int(round(x/2))*2};'
                                     for k,x in enumerate(xs)),encoding='utf-8')
        dest=MEDIA/f'part_{i:02}.mov'
        vf=(f'sendcmd=f={command.name},crop@framing={cropw}:1040:410:0,scale={W}:1020:flags=lanczos,'
            f'pad={W}:{H}:0:90:black,'
            'setsar=1,eq=contrast=1.03:saturation=1.03')
        ffmpeg.run(['-ss',str(start),'-i',str(source),'-t',str(dur),'-map','0:v:0','-map','0:a:0',
                    '-vf',vf,'-c:v','libx264','-preset','slow','-crf','16','-pix_fmt','yuv420p',
                    '-af','loudnorm=I=-16:TP=-2:LRA=9,aresample=48000','-ac','2','-c:a','pcm_s16le',
                    str(dest)],cwd=MEDIA,timeout=300)
        # Каждая сцена получает независимые события: фразы не сливаются через монтажную склейку.
        local=[subtitles.Word(round(w.start-start,3),round(w.end-start,3),w.text)
               for w in words if start<=w.start and w.end<=end and w.start<624.40]
        # Ручная редактура смысловых фраз: не оставлять одиночное «А» или «драк»
        # отдельной карточкой из-за паузы Whisper внутри предложения.
        local_events=[]
        colors=['#FFE14D','#35F2FF','#FF4A3D','#7CFF5B','#FF4FD8']
        for ra,rb in CAPTION_RANGES:
            if not start<=ra<end:
                continue
            ws=[subtitles.Word(w.start-start,w.end-start,w.text) for w in words
                if ra<=w.start<rb and w.end<=end]
            if not ws:
                continue
            top,main=subtitles._tiers(ws)
            a=ws[0].start
            tag=lambda seq:[dict(t=round(w.start-a,3),text=w.text.upper()) for w in seq]
            local_events.append(dict(start=a,end=min(dur,ws[-1].end+.30),
                color=colors[(len(events)+len(local_events))%len(colors)],top=tag(top),main=tag(main)))
        for ev,next_ev in zip(local_events,local_events[1:]):
            ev['end']=min(ev['end'],next_ev['start']+.08)
        for ev in local_events:
            ev['start']=round(ev['start']+offset,3)
            ev['end']=round(min(offset+dur,ev['end']+offset),3)
            events.append(ev)
        mapped.extend(subtitles.Word(w.start+offset,w.end+offset,w.text) for w in local)
        clips.append(dict(file=dest.resolve().as_posix(),start=offset,dur=dur,name=name,voice=True,fx={}))
        offset=round(offset+dur,2)
        print('PREPARED',i,dur,'crop range',round(float(xs.min())),round(float(xs.max())),flush=True)
    # Музыкальная подложка непрерывна через склейку; тихая под речью, поднимается после последней фразы.
    meta_file=next((ROOT/'music/auto').glob('freesound_c47*.json'))
    meta=json.loads(meta_file.read_text(encoding='utf-8'))
    track=meta_file.with_suffix('.mp3')
    music=MEDIA/'music.wav'
    ffmpeg.run(['-i',str(track),'-t',str(offset),'-af','loudnorm=I=-16:TP=-2:LRA=9',
                '-ar','48000','-ac','2','-c:a','pcm_s16le',str(music)],timeout=120)
    data=dict(name='На свадьбе никаких драк — S03E01',w=W,h=H,fps=FPS,total=offset,clips=clips,
              flashes=[],subs=events,plate=None,music=dict(file=music.resolve().as_posix(),levels=[
                  [0,-35],[.5,-18],[offset-1.1,-18],[offset-.8,-8],[offset-.3,-8],[offset,-40]]),
              sub_style=dict(top_font='Montserrat-SemiBold',main_font='Montserrat-ExtraBoldItalic',
                  top_size=42,main_size=76,y_pct=56,top_tracking=120,main_tracking=20,
                  glow_threshold=90,glow_radius=28,glow_intensity=1.3))
    (OUT/'timeline.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'mapped_words.json').write_text(json.dumps([[w.start,w.end,w.text] for w in mapped],
                                                  ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'music_credit.json').write_text(json.dumps({k:meta.get(k) for k in ['title','creator','license',
        'license_url','foreign_landing_url']},ensure_ascii=False,indent=2),encoding='utf-8')
    # Превью чистого монтажа: материал для проверки кадрирования перед запуском GUI.
    listing=MEDIA/'concat.txt'
    listing.write_text('\n'.join(f"file 'part_{i:02}.mov'" for i in range(len(clips))),encoding='utf-8')
    ffmpeg.run(['-f','concat','-safe','0','-i',str(listing),'-c:v','copy','-c:a','aac','-b:a','192k',
                '-movflags','+faststart',str(OUT/'clean_cut.mp4')],timeout=120)
    ffmpeg.run(['-i',str(OUT/'clean_cut.mp4'),'-vf',f'fps=16/{offset},scale=270:-2,tile=4x4',
                '-frames:v','1',str(OUT/'framing_review.jpg')],timeout=120)
    print('TOTAL',offset,'EVENTS',len(events),flush=True)


def render_ae():
    from editbot.ae.bridge import AfterEffects,find_ae
    data=json.loads((OUT/'timeline.json').read_text(encoding='utf-8'))
    stem='s03e01_no_fighting'
    revision=1
    while (OUT/(stem+'.mp4')).exists():
        revision+=1
        stem=f's03e01_no_fighting_v{revision}'
    target=OUT/(stem+'.mp4')
    render_to=MEDIA/(stem+'_ae.mp4')
    data['render_to']=render_to.resolve().as_posix()
    ae=AfterEffects(find_ae(),Path('C:/aebridge'),20,35)
    project=ae.build(data,OUT/(stem+'.aep'))
    ae.render(project,render_to)
    ffmpeg.run(['-i',str(render_to),'-c:v','copy','-af','loudnorm=I=-16:TP=-3:LRA=9',
                '-c:a','aac','-b:a','192k','-movflags','+faststart',str(target)],timeout=120)
    (OUT/'latest.json').write_text(json.dumps(dict(video=target.name,project=project.name,
                                  revision=revision),indent=2),encoding='utf-8')
    print('READY',target,flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',stream=sys.stdout)
    p=argparse.ArgumentParser()
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--render-ae',action='store_true')
    args=p.parse_args()
    if args.prepare: prepare()
    if args.render_ae: render_ae()
