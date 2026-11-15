"""Journal and schedule only the requested Spider-Man finals, with remote deduplication."""
import argparse
import hashlib
import logging
from datetime import datetime
import upload_approved_season as base
from editbot.state import State


def main():
    parser=argparse.ArgumentParser();parser.add_argument('keys',nargs='*')
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args()
    if not args.verify_only:
        raise RuntimeError('Publishing hold: uploads require explicit approval of the exact videos. Review AGENTS.md before changing this hold.')
    base.OUT=base.ROOT/'review/youtube_spiderman_2026-11-09'
    base.OUT.mkdir(exist_ok=True)
    path=base.OUT/'manifest.json'
    data=base.read(path) if path.exists() else dict(channel_handle='@kinomomentedit',videos=[])
    for key in args.keys:
        if any(e['key']==key for e in data['videos']):continue
        folder=base.ROOT/'review/spiderman_2002_first'/key
        file=folder/base.read(folder/'latest.json')['video']
        c=base.read(folder/'checks.json');meta=base.read(folder/'metadata_draft.json')
        assert c['file']==file.name and c['full_decode'] and c['black_events']==[]
        data['videos'].append(dict(key=key,file=str(file.resolve()),**meta,
            sha256=hashlib.file_digest(file.open('rb'),'sha256').hexdigest(),
            bytes=file.stat().st_size,duration=c['duration'],state='prepared'))
    base.save(path,data)
    cfg=base.load_config();uploader=base.UploaderAgent(cfg)
    base.preflight(cfg,uploader,data)
    inv=base.read(base.OUT/'remote_inventory.json')
    scheduled=[v for v in inv['videos'] if v.get('status',{}).get('publishAt')]
    print('REMOTE SCHEDULED',len(scheduled),flush=True)
    state=State(cfg.paths.path('state'))
    for e in data['videos']:
        if args.verify_only:continue
        if e.get('youtube_id'):continue
        assert e['state']!='uploading','Interrupted upload: reconcile remotely before retry'
        base.vpn_check(cfg)
        file=base.Path(e['file'])
        assert hashlib.file_digest(file.open('rb'),'sha256').hexdigest()==e['sha256']
        e['state']='uploading';base.save(path,data)
        yt=uploader.upload(file,base.VideoMeta(e['title'],e['description'],e['tags']),datetime.fromisoformat(e['publish_at']))
        e.update(youtube_id=yt,state='uploaded_unverified');base.save(path,data);base.report(data)
        if not state.db.execute('SELECT 1 FROM videos WHERE youtube_id=?',(yt,)).fetchone():
            state.add_video(base.ROOT/'library/Spider.Man.2002.OM.WEB-DL.AMZN.1080p-SOFCJ.mkv',file,e['title'],yt,datetime.fromisoformat(e['publish_at']))
        remote=base.api(uploader,'videos',part='snippet,status,processingDetails',id=yt)['items'][0]
        assert remote['snippet']['channelId']==data['channel_id']
        assert remote['status']['privacyStatus']=='private'
        assert datetime.fromisoformat(remote['status']['publishAt'].replace('Z','+00:00'))==datetime.fromisoformat(e['publish_at'])
        e.update(state='scheduled_verified',server_status=remote['status'],processing=remote.get('processingDetails',{}))
        base.save(path,data);base.report(data)
        print('SCHEDULED',yt,e['publish_at'],flush=True)
    ids=[e['youtube_id'] for e in data['videos']]
    remote=base.api(uploader,'videos',part='snippet,status,processingDetails',id=','.join(ids))['items']
    by_id={v['id']:v for v in remote}
    for e in data['videos']:
        v=by_id[e['youtube_id']]
        assert v['snippet']['channelId']==data['channel_id']
        assert v['status']['privacyStatus']=='private'
        assert datetime.fromisoformat(v['status']['publishAt'].replace('Z','+00:00'))==datetime.fromisoformat(e['publish_at'])
        e.update(state='scheduled_verified',server_status=v['status'],processing=v.get('processingDetails',{}))
    base.save(path,data);base.report(data)
    base.save(base.OUT/'server_verification.json',dict(checked_at=datetime.now().astimezone().isoformat(),videos=remote))
    print('VERIFIED',len(remote),'scheduled videos',flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
