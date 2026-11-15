"""Public metadata only, without cookies, tokens, downloads or mutations."""
import concurrent.futures
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import upload_approved_season as base
import yt_dlp

OUT=base.ROOT/'review/channel_diagnosis_2026-11-11'


class Quiet:
    def debug(self,*a):pass
    def warning(self,*a):pass
    def error(self,*a):pass


def extract(url,flat=False):
    opts=dict(quiet=True,no_warnings=True,logger=Quiet(),skip_download=True,
        socket_timeout=15,retries=0,extractor_retries=0,ignore_no_formats_error=True,
        noplaylist=not flat,js_runtimes={'node':{}},extract_flat=flat,playlistend=35)
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info=y.extract_info(url,download=False)
        if flat:
            return dict(url=url,entries=[{k:e.get(k) for k in ['id','title','view_count','duration','url']}
                for e in info.get('entries',[]) if e])
        return {k:info.get(k) for k in ['id','title','channel','channel_id','uploader_id','view_count',
            'like_count','comment_count','duration','availability','age_limit','upload_date','release_timestamp','live_status']}
    except Exception as e:
        return dict(url=url,error_type=type(e).__name__,message='Public metadata could not be retrieved')


if __name__=='__main__':
    OUT.mkdir(exist_ok=True)
    base.UploaderAgent(base.load_config()) # configure existing system proxy, without opening OAuth session
    ids=['9gERa0SrS_Y','jf8D7Af8VJY','ytDbbaePaa0']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        videos=list(pool.map(lambda k:extract('https://www.youtube.com/watch?v='+k),ids))
    channel=extract('https://www.youtube.com/@kinomomentedit/shorts',True)
    base.save(OUT/'public_snapshot.json',dict(checked_at=datetime.now(timezone.utc).isoformat(),videos=videos,channel=channel))
    print(json.dumps(dict(videos=videos,channel=channel),ensure_ascii=False),flush=True)
