"""Refresh public lists and transcribe six saved examples; no account access."""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import series_batch as b

ROOT=b.ROOT
SOURCE=ROOT/'reference/channel_sample_2026-11-02'
OUT=ROOT/'reference/structure_study_2026-11-11'
HANDLES=['Maven-Edits','Sanaamovie','Abyssal5','Kevedits','peter3edits','Fyzenix_Editz']


def refresh(handle):
    run=subprocess.run([sys.executable,'-m','yt_dlp','--flat-playlist','--playlist-end','12',
        '--dump-json','--skip-download','--socket-timeout','15','--retries','0',
        f'https://www.youtube.com/@{handle}/shorts'],capture_output=True,text=True,
        encoding='utf-8',errors='replace',timeout=90)
    rows=[json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
    data=dict(handle=handle,checked_at=datetime.now(timezone.utc).isoformat(),returncode=run.returncode,
        videos=[{k:r.get(k) for k in ['id','title','duration','view_count','upload_date','url']} for r in rows],
        error=run.stderr[-1500:] if run.returncode else '')
    b.save(OUT/(handle+'_public.json'),data)
    print('PUBLIC',handle,len(rows),'returncode',run.returncode,flush=True)


def transcripts():
    model=b.whisper()
    for handle in HANDLES:
        file=next((SOURCE/handle).glob('*.mp4'))
        dest=OUT/handle;dest.mkdir(exist_ok=True)
        segs,info=model.transcribe(str(file),language='en',word_timestamps=True,vad_filter=True,
            beam_size=5,condition_on_previous_text=False)
        rows=[dict(start=s.start,end=s.end,text=s.text.strip(),
                   words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segs]
        b.save(dest/'transcript.json',dict(file=str(file),language=info.language,segments=rows))
        (dest/'dialogue.txt').write_text('\n'.join(f"{r['start']:.2f}–{r['end']:.2f}: {r['text']}" for r in rows),encoding='utf-8')
        print('ASR',handle,len(rows),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['refresh','transcripts']);args=parser.parse_args()
    OUT.mkdir(exist_ok=True)
    if args.action=='refresh':
        from editbot.agents.uploader import system_proxy
        proxy=system_proxy()
        if proxy:os.environ.setdefault('HTTPS_PROXY',proxy);os.environ.setdefault('HTTP_PROXY',proxy)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(refresh,HANDLES))
    else:transcripts()
