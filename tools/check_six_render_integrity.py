"""Compare every final picture against its prepared source outside subtitle/plate areas."""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import numpy as np
import series_batch as batch
import spiderman_approved_workflow_batch as work

ROOT=batch.ROOT/'review/spiderman_six_for_approval'
OUT=ROOT/'integrity_review'


def gray(file):
    proc=subprocess.run([batch.ffmpeg.ffmpeg_exe(),'-v','error','-i',str(file),'-map','0:v:0',
        '-vf','scale=270:300:flags=area,format=gray','-vsync','0','-f','rawvideo','-'],capture_output=True,timeout=120)
    assert proc.returncode==0,proc.stderr.decode('utf-8','replace')[-1000:]
    assert len(proc.stdout)%(270*300)==0
    return np.frombuffer(proc.stdout,np.uint8).reshape(-1,300,270)


def runs(mask):
    results=[];start=None
    for i,v in enumerate(list(mask)+[False]):
        if v and start is None:start=i
        if not v and start is not None:results.append([start,i]);start=None
    return results


def audio(file):
    proc=subprocess.run([batch.ffmpeg.ffmpeg_exe(),'-v','error','-i',str(file),'-vn',
        '-ac','1','-ar','16000','-f','f32le','-'],capture_output=True,timeout=90)
    assert proc.returncode==0
    return np.frombuffer(proc.stdout,np.float32)


def audio_compare(file,timeline):
    from scipy.signal import correlate
    count=round(timeline['total']*16000)
    expected=np.zeros(count,np.float32)
    for c in timeline['clips']:
        start=round(c['start']*16000);end=round((c['start']+c['dur'])*16000)
        samples=audio(Path(c['file']))[:end-start]
        expected[start:start+len(samples)]=samples
    actual=audio(file)
    corr=correlate(actual,expected,method='fft')
    center=len(expected)-1;radius=3200
    lag=int(np.argmax(corr[center-radius:center+radius+1]))-radius
    # AE can quantize individual clip audio starts differently. Align each clip
    # independently before waveform comparison; retain measured offsets.
    part_results=[]
    for c in timeline['clips']:
        start=round(c['start']*16000);end=round((c['start']+c['dur'])*16000)
        ref=expected[start:end];got=actual[start:end]
        local=correlate(got,ref,method='fft');mid=len(ref)-1
        shift=int(np.argmax(local[mid-radius:mid+radius+1]))-radius
        aa=got[max(shift,0):];bb=ref[max(-shift,0):]
        size=min(len(aa),len(bb));aa=aa[:size];bb=bb[:size]
        scores=[];windows=[]
        for at in range(0,size-7999,8000):
            a=aa[at:at+8000];b=bb[at:at+8000]
            if float(np.sqrt(np.mean(b*b)))<.0005:continue
            score=float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-12))
            scores.append(score)
            if score<.85:windows.append(dict(start=c['start']+at/16000,correlation=round(score,4)))
        part_results.append(dict(start=c['start'],lag_ms=round(shift/16,3),
            median_correlation=round(float(np.median(scores)),5),
            minimum_correlation=round(min(scores),5),low_correlation_windows=windows))
    return dict(global_lag_ms=round(lag/16,3),parts=part_results)


def main():
    OUT.mkdir(exist_ok=True)
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8')) if (ROOT/'manifest.json').exists() else []
    by_key={r['key']:r for r in manifest}
    summary=[]
    for key in work.PLANS:
        row=by_key.get(key,{})
        folder=ROOT/key
        timeline=json.loads((folder/'timeline.json').read_text(encoding='utf-8'))
        latest=json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        file=folder/latest['video']
        digest=hashlib.file_digest(file.open('rb'),'sha256').hexdigest()
        actual=gray(file);expected=[];parts=[]
        for c in timeline['clips']:
            frames=gray(Path(c['file']))
            count=round(c['dur']*timeline['fps'])
            assert abs(len(frames)-count)<=1
            if len(frames)<count:frames=np.concatenate([frames,frames[-1:]],axis=0)
            frames=frames[:count];expected.append(frames)
            parts.append(dict(file=c['file'],actual_frames=len(frames),expected_frames=count))
        source=np.concatenate(expected,axis=0)
        n=min(len(actual),len(source))
        # All prepared pictures contain this band; captions and opening plate do not.
        x=actual[:n,90:150,10:260].astype(np.float32)
        y=source[:n,90:150,10:260].astype(np.float32)
        mae=np.abs(x-y).mean(axis=(1,2))
        changed=np.abs(x[1:]-x[:-1]).mean(axis=(1,2))
        source_changed=np.abs(y[1:]-y[:-1]).mean(axis=(1,2))
        suspicious=runs((changed<.08)&(source_changed>1.0))
        suspicious=[dict(start=a/timeline['fps'],end=(b+1)/timeline['fps']) for a,b in suspicious if b-a>=6]
        bad=[dict(start=a/timeline['fps'],end=b/timeline['fps']) for a,b in runs(mae>8) if b-a>=6]
        result=dict(key=key,video=file.name,sha256=digest,hash_matches_package=digest==row.get('sha256'),
            final_mtime=datetime.fromtimestamp(file.stat().st_mtime,timezone.utc).isoformat(),
            expected_frames=len(source),actual_frames=len(actual),frame_count_ok=abs(len(actual)-len(source))<=1,
            comparison_region='x=40..1040, y=360..600 in 1080x1200; excludes captions/plate',
            median_pixel_error=round(float(np.median(mae)),3),p95_pixel_error=round(float(np.percentile(mae,95)),3),
            maximum_pixel_error=round(float(mae.max()),3),mismatch_runs=bad,suspected_freezes=suspicious,parts=parts)
        result['audio_comparison']=audio_compare(file,timeline)
        batch.save(OUT/(key+'.json'),result)
        summary.append(result)
        print('INTEGRITY',json.dumps({k:v for k,v in result.items() if k not in {'parts','comparison_region'}},ensure_ascii=False),flush=True)
    batch.save(OUT/'summary.json',summary)


if __name__=='__main__':main()
