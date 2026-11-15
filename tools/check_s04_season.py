"""Check current local AE exports; never changes channel state."""
import hashlib
import json
import re
import subprocess
import sys
import time
import numpy as np
import s04_season_edit as edit
import check_six_render_integrity as compare
from editbot import qa


def technical():
    for candidate in edit.candidates():
        folder=edit.OUT/candidate['id']
        if not (folder/'latest.json').exists():continue
        latest=json.loads((folder/'latest.json').read_text('utf-8'))
        data=json.loads((folder/'timeline.json').read_text('utf-8'));file=folder/latest['video']
        fingerprint=hashlib.sha256((folder/'timeline.json').read_bytes()).hexdigest()
        if latest.get('timeline_sha256')!=fingerprint or latest.get('mix_version')!=2:continue
        dest=folder/'integrity.json'
        if dest.exists() and dest.stat().st_mtime>=file.stat().st_mtime and json.loads(dest.read_text('utf-8'))['file']==file.name:continue
        info=edit.b.ffmpeg.probe(file);actual=compare.gray(file)
        source=np.concatenate([compare.gray(edit.Path(c['file']))[:round(c['dur']*25)] for c in data['clips']])
        assert len(actual)==len(source)==round(data['total']*25),(file,len(actual),len(source))
        error=np.abs(actual[:,90:150,10:260].astype(np.float32)-source[:,90:150,10:260].astype(np.float32)).mean((1,2))
        assert np.percentile(error,95)<8
        assert info.has_audio and info.width==1080 and info.height==1200 and info.fps==25
        assert abs(info.duration-data['total'])<.15
        run=subprocess.run([edit.b.ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(file),'-vf',
            'blackdetect=d=0.04:pix_th=0.03:pic_th=0.99,freezedetect=n=-60dB:d=1',
            '-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=240)
        assert run.returncode==0
        black=re.findall(r'black_start:[^\r\n]+',run.stderr);freeze=re.findall(r'freeze_start:[^\r\n]+',run.stderr)
        loudness=json.loads(run.stderr[run.stderr.rfind('{'):run.stderr.rfind('}')+1])
        source_stillness=[]
        if freeze:
            source_run=subprocess.run([edit.b.ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(folder/'clean_cut.mp4'),
                '-vf','freezedetect=n=-60dB:d=1','-an','-f','null','-'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
            assert source_run.returncode==0
            source_stillness=re.findall(r'freeze_(?:start|end):[^\r\n]+',source_run.stderr)
            starts=[float(x) for x in re.findall(r'freeze_start: ([\d.]+)',source_run.stderr)]
            ends=[float(x) for x in re.findall(r'freeze_end: ([\d.]+)',source_run.stderr)]
            intervals=[(lo,next((hi for hi in ends if hi>lo),data['total'])) for lo in starts]
            for event in freeze:
                t=float(event.split(':',1)[1])
                assert any(lo-.25<=t<=hi+.25 for lo,hi in intervals),(candidate['id'],'New freeze absent in source',event)
            # The whole frame comparison above also guards against an AE hold replacing moving source footage.
        assert not black,(candidate['id'],black)
        assert float(loudness['input_tp'])<0
        audio=compare.audio_compare(file,data)
        assert all(abs(p['lag_ms'])<45 and p['median_correlation']>.85 for p in audio['parts']),(candidate['id'],audio)
        build=json.loads((folder/'build.json').read_text('utf-8'));receipt=json.loads(edit.Path(build['raw']).with_suffix('.render.json').read_text('utf-8'))
        assert receipt['returncode']==0 and abs(receipt['duration']-data['total'])<.15
        report=dict(file=file.name,sha256=hashlib.file_digest(file.open('rb'),'sha256').hexdigest(),frames=len(actual),
            fps=25,duration=info.duration,full_decode=True,p95_frame_error=round(float(np.percentile(error,95)),4),
            black_events=black,freeze_flags=[],inherited_stillness=freeze,source_stillness=source_stillness,
            loudness=loudness,audio_comparison=audio,ae_render=receipt)
        edit.b.save(dest,report)
        edit.b.ffmpeg.run(['-i',str(file),'-vf',f'fps=16/{data["total"]},scale=270:-2,tile=4x4','-frames:v','1',str(folder/'finished_sheet.jpg')],timeout=120)
        print('TECHNICAL PASS',candidate['id'],len(actual),'frames',flush=True)


def speech(model=None):
    if model is None:model=edit.b.whisper()
    for c in edit.candidates():
        folder=edit.OUT/c['id']
        if not (folder/'latest.json').exists():continue
        latest=json.loads((folder/'latest.json').read_text('utf-8'));file=folder/latest['video']
        data=json.loads((folder/'timeline.json').read_text('utf-8'));dest=folder/'rendered_transcript.json'
        fingerprint=hashlib.sha256((folder/'timeline.json').read_bytes()).hexdigest()
        if latest.get('timeline_sha256')!=fingerprint or latest.get('mix_version')!=2:continue
        assert data['subs']
        report_file=folder/'speech_check.json'
        if report_file.exists() and report_file.stat().st_mtime>=file.stat().st_mtime:
            old=json.loads(report_file.read_text('utf-8'))
            if old['file']==file.name and old['words']>0:continue
        if dest.exists() and dest.stat().st_mtime>=file.stat().st_mtime:rows=json.loads(dest.read_text('utf-8'))
        else:
            segments,_=model.transcribe(str(file),language='ru',word_timestamps=True,vad_filter=False,
                condition_on_previous_text=False,beam_size=5)
            rows=[dict(start=s.start,end=s.end,text=s.text,words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segments]
            edit.b.save(dest,rows)
        heard=[(w[0],qa._norm(w[2])) for r in rows for w in r['words']];flags=[];offsets=[];count=0
        for e in data['subs']:
            for w in e['top']+e['main']:
                word=qa._norm(w['text'])
                if len(word)<3:continue
                count+=1;t=e['start']+w['t'];h=qa._heard_at(heard,t,word)
                if h is not None:offsets.append(h-t)
                if h is None or abs(h-t)>.3:flags.append(dict(at=round(t,3),text=w['text'],heard=h,delta=round(h-t,3) if h is not None else None))
        edit.b.save(folder/'sync_flags.json',flags)
        edit.b.save(folder/'speech_check.json',dict(file=file.name,words=count,matched=len(offsets),
            median_ms=round(float(np.median(offsets))*1000) if offsets else None,
            over_300ms=int(sum(abs(x)>.3 for x in offsets)),flags=len(flags)))
        (folder/'rendered_dialogue.txt').write_text('\n'.join(f"{r['start']:.2f}–{r['end']:.2f} {r['text']}" for r in rows),encoding='utf-8')
        print('SPEECH',c['id'],'words',count,'matched',len(offsets),'flags',len(flags),flush=True)


def watch(kind):
    model=edit.b.whisper() if kind=='speech' else None
    start=time.time();previous=-1
    while time.time()-start<1800:
        if kind=='speech':speech(model)
        else:technical()
        done=0
        for c in edit.candidates():
            folder=edit.OUT/c['id'];report=folder/('speech_check.json' if kind=='speech' else 'integrity.json')
            if not report.exists() or not (folder/'latest.json').exists():continue
            latest=json.loads((folder/'latest.json').read_text('utf-8'));row=json.loads(report.read_text('utf-8'))
            fingerprint=hashlib.sha256((folder/'timeline.json').read_bytes()).hexdigest()
            if latest.get('timeline_sha256')==fingerprint and latest.get('mix_version')==2 and row['file']==latest['video']:done+=1
        if done!=previous:print('PROGRESS',kind,done,'/20',flush=True);previous=done
        if done==20:print('ALL CURRENT EXPORTS CHECKED',kind,flush=True);return
        time.sleep(3)
    raise TimeoutError('Waiting for current final exports')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    if sys.argv[1].startswith('watch_'):watch(sys.argv[1][6:])
    else:{'technical':technical,'speech':speech}[sys.argv[1]]()
