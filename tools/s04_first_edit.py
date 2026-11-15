"""First local S04 story: native source geometry, verified LostFilm captions, no publication."""
import argparse
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import series_batch as b
import remaining_season as checks

ROOT=b.ROOT
OUT=ROOT/'review/s04_first'
KEY='P301_polly_conditions'
FOLDER=OUT/KEY
SOURCE=ROOT/'library/Peaky.Blinders.s04.1080p.LostFilm/Peaky.Blinders.s04e03.BDRip.1080p.LostFilm.mkv'
FPS=25
START=451.0
END=494.6
TOTAL=43.6
MUSIC=next((ROOT/'music/auto').glob('jamendo_4c8e6da4*.mp3'))

def select():
    b.OUT=OUT;checks.OUT=OUT;checks.select=lambda key:None
    FOLDER.mkdir(parents=True,exist_ok=True)


def recognize(model,path,offset=0):
    segments,_=model.transcribe(str(path),language='ru',word_timestamps=True,vad_filter=False,
                               beam_size=5,condition_on_previous_text=False)
    return [dict(start=offset+s.start,end=offset+s.end,text=s.text.strip(),
                 words=[[offset+w.start,offset+w.end,w.word.strip()] for w in s.words or []]) for s in segments]


def study():
    wav=FOLDER/'context.wav'
    b.ffmpeg.run(['-ss','430','-i',str(SOURCE),'-t','73','-map','0:a:0','-vn','-ar','16000','-ac','1',str(wav)],timeout=90)
    rows=recognize(b.whisper(),wav,430)
    b.save(FOLDER/'context_transcript.json',rows)
    for r in rows:print(round(r['start'],2),round(r['end'],2),r['text'],flush=True)
    b.ffmpeg.run(['-ss','430','-i',str(SOURCE),'-t','73','-vf','fps=0.5,scale=320:-2,tile=6x7',
                 '-frames:v','1',str(FOLDER/'context_sheet.jpg')],timeout=90)


def prepare(reuse_captions=False):
    import av
    media=FOLDER/'media';media.mkdir(exist_ok=True)
    with av.open(str(SOURCE)) as container:
        stream=container.streams.video[0]
        assert (stream.width,stream.height)==(1920,1040) and stream.average_rate==25
    # Reframe only at an existing camera cut; audio remains one uninterrupted track.
    switch=round((456.96-START)*FPS);switch_back=round((489.4-START)*FPS);frames=round(TOTAL*FPS)
    graph=(f'[0:v]split=3[w][c][e];[w]trim=start_frame=0:end_frame={switch},setpts=PTS-STARTPTS,'
           'scale=1080:586:flags=lanczos,pad=1080:1200:0:306:black,setsar=1[v0];'
           f'[c]trim=start_frame={switch}:end_frame={switch_back},setpts=PTS-STARTPTS,'
           'crop=1440:1040:240:0,scale=1080:780:flags=lanczos,pad=1080:1200:0:210:black,setsar=1[v1];'
           f'[e]trim=start_frame={switch_back}:end_frame={frames},setpts=PTS-STARTPTS,'
           'scale=1080:586:flags=lanczos,pad=1080:1200:0:306:black,setsar=1[v2];'
           '[v0][v1][v2]concat=n=3:v=1:a=0,setsar=1[v]')
    clip=media/'part_00.mov'
    b.ffmpeg.run(['-ss',str(START),'-i',str(SOURCE),'-t',str(TOTAL),'-filter_complex',graph,
        '-map','[v]','-map','0:a:0','-r',str(FPS),'-frames:v',str(frames),'-c:v','libx264',
        '-preset','slow','-crf','16','-pix_fmt','yuv420p','-c:a','pcm_s16le','-ac','2',
        '-af',f'aresample=48000,afade=t=in:d=0.008,afade=t=out:st={TOTAL-.008}:d=0.008',str(clip)],timeout=240)
    with av.open(str(clip)) as container:
        actual=sum(1 for _ in container.decode(video=0))
    assert actual==frames,(actual,frames)
    b.ffmpeg.run(['-i',str(clip),'-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',str(FOLDER/'clean_cut.mp4')],timeout=90)
    if reuse_captions:
        prior=json.loads((FOLDER/'plan.json').read_text(encoding='utf-8'))
        assert prior['source']==str(SOURCE) and prior['start']==START and prior['end']==END and prior['audio_continuous']
        rows=json.loads((FOLDER/'clean_fragment_transcript.json').read_text(encoding='utf-8'))
    else:
        model=b.whisper();rows=[]
        for i,(a,z) in enumerate([(0,12.12),(12.12,31.08),(31.08,TOTAL)]):
            wav=media/f'speech_{i}.wav'
            b.ffmpeg.run(['-ss',str(a),'-i',str(clip),'-t',str(z-a),'-vn','-ac','1','-ar','16000',str(wav)],timeout=60)
            rows+=recognize(model,wav,a)
    for row in rows:
        row['text']=row['text'].replace('Эйда','Ада').replace('Эда','Ада')
        for word in row['words']:
            if word[2] in {'Эйда','Эда'}:word[2]='Ада'
    b.save(FOLDER/'clean_fragment_transcript.json',rows)
    words=b.fixed_words(rows)
    words=[w for w in words if 0<=w.start<w.end<=TOTAL+.02]
    events=b.captions(words,TOTAL)
    data=dict(name='Полли вернётся, но прилично себя вести не станет',w=1080,h=1200,fps=FPS,total=TOTAL,
        clips=[dict(file=clip.resolve().as_posix(),start=0,dur=TOTAL,last_frame=(frames-1)/FPS,
                    voice=True,fx={},name='Непрерывный разговор Полли и Ады')],subs=events,flashes=[],music=None,
        plate=dict(lines=['ПОЛЛИ ПОПРОСИЛИ','ВЕСТИ СЕБЯ ПРИЛИЧНО'],font='Montserrat-SemiBold',size=39,dur=3.5),
        sub_style=dict(top_font='Montserrat-SemiBold',main_font='Montserrat-ExtraBoldItalic',
            top_size=42,main_size=76,y_pct=62,top_tracking=120,main_tracking=20,
            glow_threshold=90,glow_radius=28,glow_intensity=1.3))
    b.save(FOLDER/'timeline.json',data)
    b.save(FOLDER/'plan.json',dict(source=str(SOURCE),start=START,end=END,frames=frames,fps=FPS,
        audio_map='0:a:0',audio_continuous=True,reframe_at_source=[456.96,489.4],
        crop_full_height=1040,added_music_in_ae=False,
        story=dict(setup='Просьба вести себя прилично',development='Вопрос о последствиях и цене службы компании',
                   payoff='Возвращение на своих условиях и отказ быть послушной')))
    b.save(FOLDER/'approval.json',dict(state='pending',upload_authorized=False))
    b.ffmpeg.run(['-i',str(FOLDER/'clean_cut.mp4'),'-vf',f'fps=24/{TOTAL},scale=270:-2,tile=4x6',
                 '-frames:v','1',str(FOLDER/'framing_sheet.jpg')],timeout=90)
    print('PREPARED',TOTAL,frames,'frames',len(events),'captions',flush=True)
    for row in rows:print(round(row['start'],2),round(row['end'],2),row['text'],flush=True)


def correct_final_caption_timing():
    """Use the final-file recognition to resolve the long dramatic pause."""
    data=json.loads((FOLDER/'timeline.json').read_text(encoding='utf-8'))
    rows=json.loads((FOLDER/'rendered_transcript.json').read_text(encoding='utf-8'))
    words=[w for row in rows for w in row['words'] if w[0]>=32]
    events=[e for e in data['subs'] if e['start']>=32]
    tokens=[(e,w) for e in events for group in ['top','main'] for w in e[group]]
    assert len(tokens)==len(words)
    import re
    normalize=lambda s:re.sub(r'[^а-яё]', '', s.lower())
    for (event,word),recognized in zip(tokens,words):
        assert normalize(word['text'])==normalize(recognized[2])
        word['t']=round(recognized[0]-event['start'],3)
    first=events[0];delta=round(words[0][0]-first['start'],3)
    first['start']=round(words[0][0],3)
    for group in ['top','main']:
        for word in first[group]:word['t']=round(word['t']-delta,3)
    b.save(FOLDER/'timeline.json',data)
    b.save(FOLDER/'caption_timing_correction.json',dict(source='rendered_transcript.json',
        corrected_from=32,reason='Long pause before the final refusal; avoid early word appearance'))


def variants():
    latest=json.loads((FOLDER/'latest.json').read_text(encoding='utf-8'))
    original=FOLDER/latest['video']
    target=FOLDER/(original.stem+'_music.mp4')
    if target.exists():raise FileExistsError(target)
    graph=(f'[0:a]aresample=48000,asplit=2[voice][key];'
        f'[1:a]aresample=48000,atrim=duration={TOTAL},asetpts=PTS-STARTPTS,'
        'loudnorm=I=-18:TP=-3:LRA=9,aresample=48000,highpass=f=70,lowpass=f=5500,volume=-16dB,'
        f'afade=t=in:d=1.2,afade=t=out:st={TOTAL-1}:d=1[bed];'
        '[bed][key]sidechaincompress=threshold=0.025:ratio=4:attack=20:release=300[ducked];'
        '[voice][ducked]amix=inputs=2:duration=first:normalize=0,'
        'alimiter=limit=0.708:level=false:latency=true[a]')
    b.ffmpeg.run(['-i',str(original),'-ss','20','-i',str(MUSIC),'-filter_complex',graph,
        '-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-b:a','192k','-t',str(TOTAL),
        '-movflags','+faststart',str(target)],timeout=120)
    credit=json.loads(MUSIC.with_suffix('.json').read_text(encoding='utf-8'))
    b.save(FOLDER/'music_credit.json',{k:credit[k] for k in ['title','creator','foreign_landing_url','license_url']})
    b.save(FOLDER/'variants.json',dict(original=original.name,music=target.name,preferred=None,
                                    note='Both local only; choose after review, no upload approval.'))
    print('MUSIC VARIANT',target,flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace');logging.basicConfig(level=logging.INFO)
    p=argparse.ArgumentParser();p.add_argument('action',choices=['study','prepare','render','variants','check']);p.add_argument('--reuse-captions',action='store_true');args=p.parse_args();select()
    if args.action=='study':study()
    elif args.action=='prepare':prepare(args.reuse_captions)
    elif args.action=='render':b.render(KEY)
    elif args.action=='variants':variants()
    else:checks.check_final(KEY,b.whisper())
