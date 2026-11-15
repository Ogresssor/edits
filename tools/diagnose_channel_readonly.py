"""Read-only channel diagnostics. No uploads, edits, scheduling or OAuth scope changes."""
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import upload_approved_season as base

OUT=base.ROOT/'review/channel_diagnosis_2026-11-11'


def main():
    OUT.mkdir(exist_ok=True)
    u=base.UploaderAgent(base.load_config());base.vpn_check(u.cfg)
    def get(resource,params):
        r=u.session.get(base.API+resource,params=params,timeout=60)
        if not r.ok:
            error=r.json().get('error',{})
            raise RuntimeError(str(r.status_code)+' '+error.get('message','API error'))
        return r.json()
    channel=get('channels',dict(part='snippet,contentDetails,statistics,status',mine='true',
        fields='items(id,snippet(title,customUrl),statistics,status,contentDetails/relatedPlaylists/uploads)'))['items'][0]
    assert channel['snippet']['customUrl'].lower()=='@kinomomentedit'
    records=[];page=None
    for _ in range(3):
        params=dict(part='contentDetails',playlistId=channel['contentDetails']['relatedPlaylists']['uploads'],maxResults=50)
        if page:params['pageToken']=page
        result=get('playlistItems',params)
        ids=[x['contentDetails']['videoId'] for x in result.get('items',[])]
        if ids:
            records.extend(get('videos',dict(part='snippet,status,statistics,contentDetails,processingDetails',id=','.join(ids),
                fields='items(id,snippet(title,publishedAt,channelId),status,statistics,contentDetails(duration,dimension,definition,licensedContent,regionRestriction),processingDetails/processingStatus)'))['items'])
        page=result.get('nextPageToken')
        if not page:break
    snapshot=dict(checked_at=datetime.now(timezone.utc).isoformat(),channel=channel,videos=records)
    base.save(OUT/'server_snapshot.json',snapshot)
    focus={'9gERa0SrS_Y','jf8D7Af8VJY','ytDbbaePaa0'}
    for v in records:
        if v['id'] in focus:
            print(json.dumps(v,ensure_ascii=False),flush=True)
    recent=sorted((v for v in records if v['status']['privacyStatus']=='public'),key=lambda v:v['snippet']['publishedAt'],reverse=True)[:22]
    print('RECENT PUBLICATIONS',json.dumps([dict(id=v['id'],title=v['snippet']['title'],at=v['snippet']['publishedAt'],views=v.get('statistics',{}).get('viewCount')) for v in recent],ensure_ascii=False),flush=True)
    # Detect whether internal analytics are accessible using the existing authorization only.
    r=u.session.get('https://youtubeanalytics.googleapis.com/v2/reports',params=dict(ids='channel==MINE',
        startDate='2026-11-03',endDate='2026-11-10',metrics='views',dimensions='day'),timeout=60)
    if r.ok:
        base.save(OUT/'daily_analytics.json',r.json());print('ANALYTICS AVAILABLE',flush=True)
    else:
        error=r.json().get('error',{})
        base.save(OUT/'analytics_access.json',dict(http_status=r.status_code,message=error.get('message'),reasons=[x.get('reason') for x in error.get('errors',[])]))
        print('ANALYTICS UNAVAILABLE',r.status_code,error.get('message'),flush=True)


if __name__=='__main__':main()
