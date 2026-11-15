"""Проверка готового пилота: декодирование, речь, громкость и кадры."""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot import ffmpeg, qa, subtitles
from editbot.config import load_config


def main():
    import torch
    lib=Path(torch.__file__).parent/'lib'
    os.environ['PATH']=str(lib)+os.pathsep+os.environ.get('PATH','')
    dll=os.add_dll_directory(str(lib))
    logging.basicConfig(level=logging.INFO,stream=sys.stdout)
    out=ROOT/'review/s03e01'
    latest=out/'latest.json'
    video=out/json.loads(latest.read_text())['video'] if latest.exists() else out/'s03e01_no_fighting.mp4'
    data=json.loads((out/'timeline.json').read_text(encoding='utf-8'))
    info=ffmpeg.probe(video)
    assert (info.width,info.height)==(1080,1200) and info.has_audio
    assert abs(info.duration-data['total'])<.12
    ffmpeg.run(['-i',str(video),'-f','null','-'],timeout=180)
    # Используется тот же модуль, что в исходном проекте Claude.
    rep=qa.sync_report(load_config(),video,data['subs'])
    if rep:
        rep.pop('heard',None)
    (out/'sync_report.json').write_text(json.dumps(rep,ensure_ascii=False,indent=2),encoding='utf-8')
    segs,_=subtitles._model('large-v3').transcribe(str(video),language='ru',word_timestamps=True,
              vad_filter=True,condition_on_previous_text=False)
    transcript=[dict(start=s.start,end=s.end,text=s.text,words=[[w.start,w.end,w.word] for w in s.words or []])
                for s in segs]
    (out/'rendered_transcript.json').write_text(json.dumps(transcript,ensure_ascii=False,indent=2),encoding='utf-8')
    p=subprocess.run([ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(video),'-af',
        'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],capture_output=True,
        text=True,encoding='utf-8',timeout=120)
    a,b=p.stderr.rfind('{'),p.stderr.rfind('}')
    loudness=json.loads(p.stderr[a:b+1])
    jitter=qa.jitter_report(video,data['total'])
    check=dict(duration=info.duration,width=info.width,height=info.height,fps=info.fps,
        has_audio=info.has_audio,full_decode=True,loudness=loudness,jitter_flips=jitter,
        caption_events=len(data['subs']),sync_report=rep)
    (out/'checks.json').write_text(json.dumps(check,ensure_ascii=False,indent=2),encoding='utf-8')
    ffmpeg.run(['-i',str(video),'-vf',f'fps=16/{data["total"]},scale=270:-2,tile=4x4',
        '-frames:v','1',str(out/'finished_sheet.jpg')],timeout=120)
    for t in [.8,11.5,27.5,44.4]:
        ffmpeg.run(['-ss',str(t),'-i',str(video),'-frames:v','1',str(out/f'frame_{t}.jpg')],timeout=60)
    print(json.dumps(check,ensure_ascii=False),flush=True)
    for r in transcript:
        print(round(r['start'],2),round(r['end'],2),r['text'],flush=True)


if __name__=='__main__':
    main()
