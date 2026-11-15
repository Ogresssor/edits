"""Upload only the user-approved recut, preserving existing publications."""
import hashlib
import logging
from datetime import datetime
import upload_approved_season as base


def main():
    base.OUT=base.ROOT/'review/youtube_choose_fear_test_2026-11-04'
    base.OUT.mkdir(exist_ok=True)
    manifest=base.OUT/'manifest.json'
    if manifest.exists():
        data=base.read(manifest)
    else:
        folder=base.ROOT/'review/choose_fear_rework_2026-11-04/choose_fear_recut'
        checks=base.read(folder/'checks.json')
        file=folder/base.read(folder/'latest.json')['video']
        assert checks['file']==file.name and checks['full_decode'] and checks['black_events']==[]
        meta=base.read(base.ROOT/'review/s03_remaining/e03_choose_fear/metadata_draft.json')
        entry=dict(key='choose_fear_recut',file=str(file.resolve()),
            title='Неверный выбор — и до 11:44 не доживёте | Острые козырьки',
            description=meta['description'],tags=meta.get('tags',['Острые козырьки','Peaky Blinders','Томми Шелби','Shorts']),episode=3,
            duration=checks['duration'],bytes=file.stat().st_size,
            sha256=hashlib.file_digest(file.open('rb'),'sha256').hexdigest(),state='prepared')
        data=dict(channel_handle='@kinomomentedit',videos=[entry],
            experiment='Same scene, reduced pauses; original publication retained')
        base.save(manifest,data)
    cfg=base.load_config()
    uploader=base.UploaderAgent(cfg)
    base.preflight(cfg,uploader,data)
    base.upload(cfg,uploader,data)
    entry=data['videos'][0]
    remote=base.api(uploader,'videos',part='snippet,status,processingDetails',id=entry['youtube_id'])['items'][0]
    assert remote['snippet']['channelId']==data['channel_id']
    assert datetime.fromisoformat(remote['status']['publishAt'].replace('Z','+00:00'))==datetime.fromisoformat(entry['publish_at'])
    base.save(base.OUT/'server_verification.json',remote)
    print('TEST VERIFIED',entry['youtube_id'],entry['publish_at'],flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
