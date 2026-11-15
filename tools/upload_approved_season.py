"""Upload only the eleven user-approved final versions; journal every server ID."""
import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot.config import load_config
from editbot.agents.uploader import UploaderAgent, QuotaExceeded
from editbot.agents import uploader as uploader_module
from editbot.agents.metadata import VideoMeta
from editbot import ffmpeg

OUT=ROOT/'review/youtube_upload_2026-11-02'
MSK=timezone(timedelta(hours=3),'MSK')
API='https://www.googleapis.com/youtube/v3/'
# Reading channel identity and scheduled status requires the additional read-only scope.
uploader_module.SCOPES=[
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path,data):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)


def prepare():
    OUT.mkdir(exist_ok=True)
    if (OUT/'manifest.json').exists():
        return read(OUT/'manifest.json')
    tags=['Острые козырьки','Peaky Blinders','Томми Шелби','Shorts']
    entries=[]
    def add(key,file,title,episode,description,credit,tags=tags):
        file=Path(file)
        assert file.is_file(),file
        info=ffmpeg.probe(file)
        assert info.has_audio and (info.width,info.height)==(1080,1200)
        desc=(description+f'\nОстрые козырьки / Peaky Blinders. Сезон 3, серия {episode}.\n\n'
            f"Музыка: {credit['title']} — {credit['creator']}.\n"
            f"Источник: {credit['foreign_landing_url']}\nЛицензия: {credit['license_url']}\n\n"
            '#ОстрыеКозырьки #PeakyBlinders #Shorts')
        entries.append(dict(key=key,file=str(file.resolve()),title=title,description=desc,tags=tags,
            episode=episode,duration=info.duration,bytes=file.stat().st_size,
            sha256=hashlib.file_digest(file.open('rb'),'sha256').hexdigest(),state='prepared'))
    f=ROOT/'review/s03e01'
    add('e01_no_fighting',f/read(f/'latest.json')['video'],
        '«Никаких драк». Первым нарушил Томми | Острые козырьки',1,
        'Томми требует спокойствия на своей свадьбе. Но собственное правило выдерживает недолго.',
        read(f/'music_credit.json'))
    for item in read(ROOT/'review/s03e01_batch/manifest.json'):
        key=item['key']; f=ROOT/'review/s03e01_batch'/key
        meta=read(f/'metadata_draft.json')
        if key=='01_grace_polly':
            accepted=read(f/'accepted.json')
            file=f/accepted['video']
            credit=next(x['added_music'] for x in read(f/'music_alternatives.json') if x['file']==file.name)
        else:
            file=f/read(f/'latest.json')['video']; credit=read(f/'music_credit.json')
        description=meta['description'].split('\nОстрые козырьки')[0]
        add('e01_'+key,file,meta['title'],1,description,credit,meta['tags'])
    for item in read(ROOT/'review/s03_remaining/manifest.json'):
        f=ROOT/'review/s03_remaining'/item['key']; meta=read(f/'metadata_draft.json')
        add(item['key'],f/item['video'],meta['title'],item['episode'],item['title']+'.',read(f/'music_credit.json'))
    assert len(entries)==11 and len({x['sha256'] for x in entries})==11
    data=dict(channel_handle='@kinomomentedit',publish_times_msk=['09:00','13:00','17:00'],videos=entries)
    save(OUT/'manifest.json',data)
    return data


def vpn_check(cfg):
    import requests
    want=cfg.youtube.get('vpn')
    if not want:
        return
    # Never send an OAuth-bearing AuthorizedSession to the IP-check service.
    r=requests.get('https://ipinfo.io/json',timeout=20)
    r.raise_for_status()
    ip=r.json()
    for key in ['country','org']:
        expected=want.get(key)
        if expected and not str(ip.get(key,'')).startswith(str(expected)):
            raise RuntimeError(f'VPN_MISMATCH: {key} does not match configured VPN')
    print('VPN verified: configured country and provider',flush=True)


def api(uploader,resource,**params):
    fields={
        'channels':'items(id,snippet(title,customUrl),contentDetails/relatedPlaylists/uploads)',
        'playlistItems':'nextPageToken,items/contentDetails/videoId',
        'videos':'items(id,snippet(title,channelId),status,processingDetails/processingStatus)',
    }
    if resource in fields:
        params.setdefault('fields',fields[resource])
    r=uploader.session.get(API+resource,params=params,timeout=60)
    if not r.ok:
        err=r.json().get('error',{})
        reasons=[x.get('reason') for x in err.get('errors',[])]
        raise RuntimeError(f'API_READ_FAILED: {resource}: HTTP {r.status_code}: {reasons}')
    return r.json()


def preflight(cfg,uploader,data):
    vpn_check(cfg)
    channel=api(uploader,'channels',part='id,snippet,contentDetails',mine='true')['items']
    if len(channel)!=1:
        raise RuntimeError('Expected exactly one authenticated channel')
    channel=channel[0]
    handle=channel['snippet'].get('customUrl','').lower()
    assert handle==data['channel_handle'].lower(),f'Wrong channel handle: {handle}'
    print('CHANNEL VERIFIED',channel['snippet']['title'],handle,channel['id'],flush=True)
    videos=[]
    playlist=channel['contentDetails']['relatedPlaylists']['uploads']
    page=None
    for _ in range(8):
        params=dict(part='contentDetails',playlistId=playlist,maxResults=50)
        if page: params['pageToken']=page
        result=api(uploader,'playlistItems',**params)
        ids=[x['contentDetails']['videoId'] for x in result.get('items',[])]
        if ids:
            videos.extend(api(uploader,'videos',part='snippet,status,processingDetails',id=','.join(ids))['items'])
        page=result.get('nextPageToken')
        if not page: break
    save(OUT/'remote_inventory.json',dict(channel_id=channel['id'],checked_at=datetime.now(timezone.utc).isoformat(),videos=videos))
    occupied=[]
    for v in videos:
        at=v.get('status',{}).get('publishAt')
        if at: occupied.append(datetime.fromisoformat(at.replace('Z','+00:00')))
    db=sqlite3.connect(cfg.paths.path('state').resolve().as_uri()+'?mode=ro',uri=True)
    rows=db.execute('SELECT youtube_id,publish_at,file,title FROM videos WHERE youtube_id IS NOT NULL').fetchall()
    db.close()
    for yt,at,_,_ in rows:
        if yt and re.fullmatch(r'[A-Za-z0-9_-]{11}',yt) and at:
            dt=datetime.fromisoformat(at)
            occupied.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    def norm(t): return ' '.join(t.lower().replace('ё','е').split())
    remote_titles={norm(v['snippet']['title']):v for v in videos}
    after=datetime.now(timezone.utc)+timedelta(minutes=30)
    for entry in data['videos']:
        found=remote_titles.get(norm(entry['title']))
        if found:
            entry.update(state='already_uploaded',youtube_id=found['id'],publish_at=found.get('status',{}).get('publishAt'))
            continue
        if entry.get('youtube_id'): continue
        # Preserve reserved slots across restart; otherwise allocate in exact Moscow time.
        if entry.get('publish_at') and datetime.fromisoformat(entry['publish_at'])>after:
            occupied.append(datetime.fromisoformat(entry['publish_at']))
            continue
        start=datetime.now(MSK).replace(hour=0,minute=0,second=0,microsecond=0)
        for day in range(400):
            candidates=[(start+timedelta(days=day)).replace(hour=h).astimezone(timezone.utc) for h in [9,13,17]]
            slot=next((x for x in candidates if x>=after and all(abs((x-y).total_seconds())>=3600 for y in occupied)),None)
            if slot:
                entry['publish_at']=slot.isoformat(); occupied.append(slot); break
        else: raise RuntimeError('No available publication slot')
    data['channel_id']=channel['id']
    save(OUT/'manifest.json',data)
    report(data)
    print('PREFLIGHT COMPLETE',len(data['videos']),flush=True)


def report(data):
    lines=['# Загрузка одобренных роликов','',f"Канал: {data['channel_handle']}",'',
        '| Ролик | Публикация МСК | Статус | YouTube |','|---|---|---|---|']
    for x in data['videos']:
        at=x.get('publish_at')
        when=datetime.fromisoformat(at.replace('Z','+00:00')).astimezone(MSK).strftime('%d.%m.%Y %H:%M') if at else '—'
        url=f"[Открыть](https://youtu.be/{x['youtube_id']})" if x.get('youtube_id') else '—'
        label='Блокировка по заявке (сообщил пользователь)' if x.get('restriction_reported') else x['state']
        safe_title=x['title'].replace('|','\\|')
        lines.append(f"| {safe_title} | {when} | {label} | {url} |")
    lines.extend(['','Проверка Data API подтверждает наличие, обработку и время публикации. '
        'Она не подтверждает отсутствие заявок Content ID или доступность во всех странах.'])
    (OUT/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def upload(cfg,uploader,data):
    from editbot.state import State
    state=State(cfg.paths.path('state'))
    for entry in data['videos']:
        if entry.get('youtube_id'): continue
        assert entry['state']!='uploading','Interrupted upload: reconcile remote inventory before retry'
        file=Path(entry['file'])
        assert hashlib.file_digest(file.open('rb'),'sha256').hexdigest()==entry['sha256']
        vpn_check(cfg)
        entry['state']='uploading'; save(OUT/'manifest.json',data)
        meta=VideoMeta(entry['title'],entry['description'],entry['tags'])
        when=datetime.fromisoformat(entry['publish_at'])
        try:
            yt=uploader.upload(file,meta,when)
        except QuotaExceeded:
            entry['state']='quota_wait'; save(OUT/'manifest.json',data); report(data)
            print('QUOTA: remaining files preserved in manifest',flush=True)
            return
        entry.update(youtube_id=yt,state='uploaded_unverified')
        save(OUT/'manifest.json',data); report(data)
        source=next((ROOT/'library/Peaky.Blinders.S03.1080p.BluRay').glob(f"*S03E{entry['episode']:02}*.mkv"))
        if not state.db.execute('SELECT 1 FROM videos WHERE youtube_id=?',(yt,)).fetchone():
            state.add_video(source,file,entry['title'],yt,when)
        remote=api(uploader,'videos',part='snippet,status,processingDetails',id=yt)['items'][0]
        assert remote['snippet']['channelId']==data['channel_id']
        status=remote['status']
        assert status['privacyStatus']=='private' and status.get('publishAt')
        assert datetime.fromisoformat(status['publishAt'].replace('Z','+00:00'))==when
        entry.update(state='scheduled_verified',server_status=status,
            processing=remote.get('processingDetails',{}))
        save(OUT/'manifest.json',data); report(data)
        print('SCHEDULED',yt,when.astimezone(MSK).isoformat(),entry['title'],flush=True)
    print('COMPLETE',sum(bool(x.get('youtube_id')) for x in data['videos']),flush=True)


def verify(uploader,data):
    ids=[x['youtube_id'] for x in data['videos'] if x.get('youtube_id')]
    remote=api(uploader,'videos',part='snippet,status,processingDetails',id=','.join(ids))['items']
    by_id={x['id']:x for x in remote}
    failed=[]
    for entry in data['videos']:
        v=by_id.get(entry.get('youtube_id'))
        if not v:
            failed.append(entry['key']+': not present on server'); continue
        assert v['snippet']['channelId']==data['channel_id']
        status=v['status']; processing=v.get('processingDetails',{})
        entry.update(server_status=status,processing=processing)
        if status.get('uploadStatus') in {'failed','rejected','deleted'} or processing.get('processingStatus')=='failed':
            failed.append(entry['key']+': '+str(status.get('rejectionReason') or status.get('failureReason') or processing))
        elif status.get('privacyStatus')=='private' and status.get('publishAt'):
            assert datetime.fromisoformat(status['publishAt'].replace('Z','+00:00'))==datetime.fromisoformat(entry['publish_at'])
            entry['state']='scheduled_verified'
        else:
            failed.append(entry['key']+': unexpected privacy or schedule')
    data['verified_at']=datetime.now(timezone.utc).isoformat()
    data['verification_errors']=failed
    save(OUT/'manifest.json',data); report(data)
    save(OUT/'server_verification.json',dict(checked_at=data['verified_at'],videos=remote,errors=failed))
    assert len(ids)==11 and not failed, failed
    print('VERIFIED ON YOUTUBE',len(remote),'scheduled videos',flush=True)
    for entry in data['videos']:
        print(entry['youtube_id'],entry['publish_at'],entry['processing'].get('processingStatus'),flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING,format='%(message)s',stream=sys.stdout)
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','preflight','upload','authorize-upload','verify'])
    args=parser.parse_args()
    data=prepare()
    if args.action=='prepare':
        print('PREPARED',len(data['videos']),round(sum(x['bytes'] for x in data['videos'])/2**20),'MiB')
    else:
        cfg=load_config(); uploader=UploaderAgent(cfg)
        if args.action=='authorize-upload':
            vpn_check(cfg)
            uploader.authorize()
        if args.action=='verify':
            vpn_check(cfg)
            verify(uploader,data)
        else:
            preflight(cfg,uploader,data)
            if args.action in {'upload','authorize-upload'}:
                logging.getLogger('uploader').setLevel(logging.INFO)
                upload(cfg,uploader,data)
