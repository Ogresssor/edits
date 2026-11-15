"""Schedule only the five reviewed Peaky finals, ahead of three existing Spider uploads."""
import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
import upload_approved_season as b
from editbot.state import State

REVIEW=b.ROOT/'review/s03_stories_batch2'
OUT=b.ROOT/'review/youtube_peaky_priority_2026-11-11'
ORDER=['08_e06_alfie_limits','07_e05_johnny_heaven','05_e04_polly_no_regret',
       '02_e03_charlie_mother','04_e03_becoming_father']
SPIDER=['rNu4tgKnmk8','_e6Tw3bdcKw','GSF_1kCyYHY']
SCOPES=b.uploader_module.SCOPES+['https://www.googleapis.com/auth/youtube.force-ssl']
APPROVAL='проверь козырьков если они нормальные то давай сначала их'


def inventory(u):
    channels=b.api(u,'channels',part='id,snippet,contentDetails',mine='true')['items']
    assert len(channels)==1 and channels[0]['snippet']['customUrl'].lower()=='@kinomomentedit'
    ch=channels[0];videos=[];page=None
    while True:
        params=dict(part='contentDetails',playlistId=ch['contentDetails']['relatedPlaylists']['uploads'],maxResults=50)
        if page:params['pageToken']=page
        rows=b.api(u,'playlistItems',**params)
        ids=[x['contentDetails']['videoId'] for x in rows.get('items',[])]
        if ids:videos+=b.api(u,'videos',part='snippet,status,processingDetails',id=','.join(ids))['items']
        page=rows.get('nextPageToken')
        if not page:break
    b.save(OUT/'remote_inventory.json',dict(channel_id=ch['id'],videos=videos,
        checked_at=datetime.now(timezone.utc).isoformat()))
    return ch['id'],videos


def prepare(cfg,u):
    path=OUT/'manifest.json'
    if path.exists():return b.read(path)
    channel,videos=inventory(u);byid={v['id']:v for v in videos}
    reviewed={r['key']:r for r in b.read(REVIEW/'manifest.json')}
    audits={r['key']:r for r in b.read(REVIEW/'review_2026-11-11/summary.json')}
    entries=[]
    for key in ORDER:
        row=reviewed[key];audit=audits[key];file=REVIEW/row['review_file']
        digest=hashlib.file_digest(file.open('rb'),'sha256').hexdigest()
        assert audit['selected'] and audit['technical_pass'] and audit['sha256']==digest
        meta=b.read(REVIEW/key/'metadata_draft.json')
        assert not any(meta['title']==v['snippet']['title'] for v in videos),'Already uploaded: '+key
        entries.append(dict(key=key,file=str(file.resolve()),sha256=digest,bytes=file.stat().st_size,
                            duration=audit['duration'],episode=row['episode'],state='prepared',**meta))
    moves=[]
    for vid in SPIDER:
        v=byid[vid];status=v['status']
        if status['privacyStatus']=='public':continue
        assert status['privacyStatus']=='private' and status.get('publishAt')
        moves.append(dict(youtube_id=vid,title=v['snippet']['title'],original_status=status,state='planned'))
    moved={r['youtube_id'] for r in moves}
    occupied=[datetime.fromisoformat(v['status']['publishAt'].replace('Z','+00:00'))
              for v in videos if v['id'] not in moved and v['status'].get('publishAt')]
    start=datetime.now(b.MSK).replace(hour=0,minute=0,second=0,microsecond=0)
    slots=[];after=datetime.now(timezone.utc)+timedelta(minutes=30)
    for day in range(30):
        for hour in [9,13,17]:
            dt=(start+timedelta(days=day)).replace(hour=hour).astimezone(timezone.utc)
            if dt>after and all(abs((dt-x).total_seconds())>=3600 for x in occupied):slots.append(dt)
    assert len(slots)>=len(entries)+len(moves)
    for row,dt in zip(entries+moves,slots):row['publish_at']=dt.isoformat()
    data=dict(channel_handle='@kinomomentedit',channel_id=channel,videos=entries,moves=moves,
              authorization=dict(user_message=APPROVAL,files={e['key']:e['sha256'] for e in entries}))
    b.save(path,data);b.report(data)
    print('PLAN',json.dumps([(r.get('key',r.get('youtube_id')),r['publish_at']) for r in entries+moves]),flush=True)
    return data


def authorize(u):
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow=InstalledAppFlow.from_client_secrets_file(str(u.y_path('client_secrets')),SCOPES)
    creds=flow.run_local_server(port=8765,prompt='consent',open_browser=True,
        authorization_prompt_message='',timeout_seconds=600,
        success_message='YouTube подключён. Очередь будет обновлена; вкладку можно закрыть.')
    assert creds and creds.valid,'Google sign-in did not finish'
    u.y_path('token').write_text(creds.to_json(),encoding='utf-8')
    u._session=None
    b.uploader_module.SCOPES=SCOPES


def verify_one(u,channel,vid,when):
    # Read-after-write responses can briefly contain the previous schedule.
    for attempt in range(6):
        v=b.api(u,'videos',part='snippet,status,processingDetails',id=vid)['items'][0]
        assert v['snippet']['channelId']==channel
        s=v['status']
        if (s['privacyStatus']=='private' and s.get('publishAt')
                and datetime.fromisoformat(s['publishAt'].replace('Z','+00:00'))==datetime.fromisoformat(when)):break
        if attempt==5:raise RuntimeError('Server schedule not confirmed for '+vid)
        time.sleep(2)
    assert s.get('uploadStatus') not in {'failed','rejected','deleted'}
    assert v.get('processingDetails',{}).get('processingStatus')!='failed'
    return v


def run(cfg,u,data):
    channel,videos=inventory(u);assert channel==data['channel_id']
    byid={v['id']:v for v in videos}
    # Move the existing uploads first, and verify each server result before filling their slots.
    for row in data['moves']:
        vid=row['youtube_id'];v=byid[vid]
        status=v['status'];assert status['privacyStatus']=='private'
        target=datetime.fromisoformat(row['publish_at'])
        assert target>datetime.now(timezone.utc)+timedelta(minutes=10)
        writable={k:status[k] for k in ['privacyStatus','license','embeddable','publicStatsViewable',
                  'selfDeclaredMadeForKids','containsSyntheticMedia'] if k in status}
        writable.update(privacyStatus='private',publishAt=target.isoformat())
        if datetime.fromisoformat(status['publishAt'].replace('Z','+00:00'))!=target:
            r=u.session.put(b.API+'videos',params={'part':'status'},json={'id':vid,'status':writable},timeout=60)
            if not r.ok:
                reasons=[x.get('reason') for x in r.json().get('error',{}).get('errors',[])]
                raise RuntimeError(f'Schedule change failed: HTTP {r.status_code} {reasons}')
            row['update_response']=r.json().get('status',{})
            b.save(OUT/'manifest.json',data)
        remote=verify_one(u,channel,vid,row['publish_at'])
        row.update(state='rescheduled_verified',server_status=remote['status'])
        b.save(OUT/'manifest.json',data)
        print('MOVED',vid,row['publish_at'],flush=True)
    state=State(cfg.paths.path('state'))
    for e in data['videos']:
        if e.get('youtube_id'):continue
        assert e['state']!='uploading','Interrupted upload: reconcile remotely before retry'
        assert not any(e['title']==v['snippet']['title'] for v in videos),'Possible duplicate'
        file=b.Path(e['file']);assert hashlib.file_digest(file.open('rb'),'sha256').hexdigest()==e['sha256']
        when=datetime.fromisoformat(e['publish_at']);assert when>datetime.now(timezone.utc)
        b.vpn_check(cfg);e['state']='uploading';b.save(OUT/'manifest.json',data)
        try:
            yt=u.upload(file,b.VideoMeta(e['title'],e['description'],e['tags']),when)
        except b.QuotaExceeded:
            e['state']='quota_wait'
            e['last_error']='YouTube daily quota/upload limit; no completed server ID received'
            b.save(OUT/'manifest.json',data);b.report(data)
            print('QUOTA_WAIT',e['key'],flush=True)
            break
        e.update(youtube_id=yt,state='uploaded_unverified');b.save(OUT/'manifest.json',data);b.report(data)
        remote=verify_one(u,channel,yt,e['publish_at'])
        e.update(state='scheduled_verified',server_status=remote['status'],processing=remote.get('processingDetails',{}))
        b.save(OUT/'manifest.json',data);b.report(data)
        if not state.db.execute('SELECT 1 FROM videos WHERE youtube_id=?',(yt,)).fetchone():
            source=b.Path(b.read(REVIEW/e['key']/'plan.json')['source'])
            state.add_video(source,file,e['title'],yt,when)
        print('SCHEDULED',yt,when.astimezone(b.MSK).isoformat(),flush=True)
    verify(u,data)


def verify(u,data):
    rows=[];pending=[]
    for e in data['videos']+data['moves']:
        if not e.get('youtube_id'):
            pending.append(e['key']);continue
        remote=verify_one(u,data['channel_id'],e['youtube_id'],e['publish_at']);rows.append(remote)
        e['server_status']=remote['status'];e['processing']=remote.get('processingDetails',{})
    b.save(OUT/'manifest.json',data);b.report(data)
    b.save(OUT/'server_verification.json',dict(checked_at=datetime.now(timezone.utc).isoformat(),videos=rows,pending=pending))
    # Keep the Spider upload journal consistent with the server's new schedule.
    spider_path=b.ROOT/'review/youtube_spiderman_six_2026-11-11/manifest.json'
    spider=b.read(spider_path)
    for moved in data['moves']:
        for row in spider['videos']:
            if row.get('youtube_id')==moved['youtube_id']:
                row.update(publish_at=moved['publish_at'],server_status=moved['server_status'],state='scheduled_verified')
    b.save(spider_path,spider)
    state=State(b.load_config().paths.path('state'))
    for moved in data['moves']:
        state.db.execute('UPDATE videos SET publish_at=? WHERE youtube_id=?',
                         (moved['publish_at'],moved['youtube_id']))
    state.db.commit();state.db.close()
    selected={e['key']:e for e in data['videos'] if e.get('youtube_id')}
    review_rows=b.read(REVIEW/'manifest.json')
    for row in review_rows:
        if row['key'] in selected:
            e=selected[row['key']]
            row.update(approval='approved_after_review',uploaded=True,youtube_id=e['youtube_id'],publish_at=e['publish_at'])
            b.save(REVIEW/e['key']/'approval.json',dict(state='approved_after_review',upload_authorized=True,
                sha256=e['sha256'],user_message=APPROVAL,youtube_id=e['youtube_id']))
    b.save(REVIEW/'manifest.json',review_rows)
    report_path=OUT/'README.md'
    text=report_path.read_text(encoding='utf-8')
    text+='\n## Паук перенесён после этой партии\n\n| Ролик | Публикация МСК | YouTube |\n|---|---|---|\n'
    for moved in data['moves']:
        when=datetime.fromisoformat(moved['publish_at']).astimezone(b.MSK).strftime('%d.%m.%Y %H:%M')
        title=moved['title'].replace('|','\\|')
        text+=f"| {title} | {when} | [Открыть](https://youtu.be/{moved['youtube_id']}) |\n"
    text+='\nДва других ролика «Козырьков» оставлены вне очереди. [Оценки и проверка](../s03_stories_batch2/review_2026-11-11/README.md).\n'
    report_path.write_text(text,encoding='utf-8')
    b.OUT=spider_path.parent;b.report(spider);b.OUT=OUT
    print('VERIFIED',len(selected),'of',len(data['videos']),'Peaky uploads;',len(data['moves']),'Spider moves; pending',pending,flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','authorize-run','run','verify'])
    args=parser.parse_args();OUT.mkdir(exist_ok=True);b.OUT=OUT
    cfg=b.load_config();u=b.UploaderAgent(cfg);b.vpn_check(cfg)
    data=prepare(cfg,u)
    if args.action=='prepare':return
    if args.action=='authorize-run':authorize(u)
    if args.action=='verify':verify(u,data)
    else:
        run(cfg,u,data)
        if any(not e.get('youtube_id') for e in data['videos']):
            raise SystemExit(75)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace');logging.basicConfig(level=logging.INFO)
    logging.getLogger('google_auth_oauthlib.flow').setLevel(logging.WARNING)
    main()
