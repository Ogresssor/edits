"""Upload exactly two selected S04 exports to verified free slots on November 14."""
import argparse
import hashlib
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
import prioritize_peaky_backlog as p

b=p.b
OUT=b.ROOT/'review/youtube_s04_nov14_pair'
SOURCE=b.ROOT/'review/s04_season'
SELECTION=[('P303','2026-11-14T09:00:00+03:00'),('P403','2026-11-14T17:00:00+03:00')]


def prepare():
    OUT.mkdir(exist_ok=True)
    dest=OUT/'manifest.json'
    if dest.exists():return b.read(dest)
    catalog={r['key']:r for r in b.read(SOURCE/'manifest.json')}
    videos=[]
    for key,when in SELECTION:
        row=catalog[key];folder=SOURCE/key;file=SOURCE/row['video'];integrity=b.read(folder/'integrity.json')
        digest=hashlib.file_digest(file.open('rb'),'sha256').hexdigest()
        assert digest==row['sha256']==integrity['sha256'] and integrity['full_decode']
        assert not integrity['black_events'] and not integrity['freeze_flags']
        credit=b.read(folder/'music_credit.json')
        description=(row['editorial_note']+f"\nОстрые козырьки / Peaky Blinders. Сезон 4, серия {row['episode']}.\n\n"
            f"Музыка: {credit['title']} — {credit['creator']}.\nИсточник: {credit['foreign_landing_url']}\n"
            f"Лицензия: {credit['license_url']}\n\n#ОстрыеКозырьки #PeakyBlinders #Shorts")
        videos.append(dict(key=key,title=row['title']+' | Острые козырьки',description=description,
            tags=['Острые козырьки','Peaky Blinders','Shorts'],file=str(file.resolve()),sha256=digest,
            duration=row['seconds'],publish_at=when,state='prepared_awaiting_selection'))
    data=dict(channel_id='UCqCpOSGb7KYhSDXHNVivCFw',channel_handle='@kinomomentedit',videos=videos,
        authorization=dict(confirmed=False,scope='Only these exact two exports and dates; no other queue changes'))
    b.save(dest,data)
    (OUT/'REVIEW.md').write_text('# Два свободных слота 14 ноября\n\n'
        'Пара подготовлена; ожидается выбор пользователя, загрузка пока запрещена.\n\n'+
        '\n'.join(f"- {v['publish_at']}: {v['title']} — {v['duration']} с; `{v['sha256']}`" for v in videos)+'\n',encoding='utf-8')
    return data


def run(data):
    assert data['authorization'].get('confirmed') is True,'Exact pair selection is required by AGENTS.md'
    assert data['authorization'].get('files')=={v['key']:v['sha256'] for v in data['videos']}
    cfg=b.load_config();u=b.UploaderAgent(cfg);b.vpn_check(cfg);p.OUT=OUT
    channel,remote=p.inventory(u);assert channel==data['channel_id']
    assert len(data['videos'])==2
    known={v.get('youtube_id') for v in data['videos'] if v.get('youtube_id')}
    for v in data['videos']:
        when=datetime.fromisoformat(v['publish_at']);assert when.date().isoformat()=='2026-11-14'
        assert when>datetime.now(timezone.utc)
        if not v.get('youtube_id'):
            assert v['state']!='uploading','Interrupted request: reconcile remotely before another insert'
            assert not any(x['snippet']['title']==v['title'] for x in remote),'Possible duplicate title'
            for other in remote:
                date=other['status'].get('publishAt')
                if date and other['id'] not in known:
                    assert abs((datetime.fromisoformat(date.replace('Z','+00:00'))-when).total_seconds())>=3600,'Slot is occupied'
            file=Path(v['file']);assert hashlib.file_digest(file.open('rb'),'sha256').hexdigest()==v['sha256']
            v['state']='uploading';b.save(OUT/'manifest.json',data)
            try:v['youtube_id']=u.upload(file,b.VideoMeta(v['title'],v['description'],v['tags']),when)
            except b.QuotaExceeded:
                v['state']='quota_wait';b.save(OUT/'manifest.json',data)
                v['last_error']='YouTube rejected upload because of daily quota/upload limit; no completed video ID received'
                b.save(OUT/'manifest.json',data);sync_review(data)
                print('QUOTA_WAIT',v['key'],flush=True);return 75
            v['state']='uploaded_unverified';b.save(OUT/'manifest.json',data)
        result=p.verify_one(u,channel,v['youtube_id'],v['publish_at'])
        v.update(state='scheduled_verified',server_status=result['status'],processing=result.get('processingDetails',{}))
        b.save(OUT/'manifest.json',data)
        sync_review(data)
        print('SCHEDULED',v['key'],v['youtube_id'],v['publish_at'],flush=True)
    p.inventory(u)
    return 0


def sync_review(data):
    catalog_path=SOURCE/'manifest.json';catalog=b.read(catalog_path)
    for entry in data['videos']:
        for row in catalog:
            if row['key']!=entry['key']:continue
            assert row['sha256']==entry['sha256']
            row.update(approval='approved_for_scheduled_upload',upload_authorized=True,
                upload_state=entry['state'],publish_at=entry['publish_at'],uploaded=bool(entry.get('youtube_id')))
            if entry.get('youtube_id'):row['youtube_id']=entry['youtube_id']
            b.save(SOURCE/entry['key']/'approval.json',dict(state='approved_for_scheduled_upload',
                upload_authorized=True,sha256=entry['sha256'],user_message=data['authorization']['user_message'],
                publish_at=entry['publish_at'],upload_state=entry['state'],youtube_id=entry.get('youtube_id')))
    b.save(catalog_path,catalog)
    (OUT/'README.md').write_text('# Догрузка на 14 ноября\n\n'
        'Пользователь подтвердил конкретную пару: «Линда и Ада — подготовленная пара».\n\n'+
        '\n'.join(f"- {v['publish_at']}: {v['title']} — {v['state']}"+
            (f"; https://youtu.be/{v['youtube_id']}" if v.get('youtube_id') else '') for v in data['videos'])+'\n',encoding='utf-8')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','run']);args=parser.parse_args()
    data=prepare()
    if args.action=='run':raise SystemExit(run(data))
    else:print('PREPARED ONLY; awaiting exact pair selection')
