"""Upload the six exact finals explicitly approved on 2026-11-11."""
import argparse
import hashlib
import logging
from datetime import datetime, timezone

import upload_approved_season as base
from editbot.state import State

REVIEW = base.ROOT / 'review/spiderman_six_for_approval'
OUT = base.ROOT / 'review/youtube_spiderman_six_2026-11-11'
KEYS = ['01_name', '02_photos', '03_fired', '04_photographer', '05_web', '06_fire']
APPROVAL = 'давай заливай всё так же по очереди'


def prepare():
    OUT.mkdir(exist_ok=True)
    path = OUT / 'manifest.json'
    if path.exists():
        return base.read(path)
    reviewed = base.read(REVIEW / 'manifest.json')
    assert [r['key'] for r in reviewed] == KEYS
    entries = []
    for row in reviewed:
        file = REVIEW / row['review_file']
        digest = hashlib.file_digest(file.open('rb'), 'sha256').hexdigest()
        assert digest == row['sha256']
        checks = row['checks']
        assert checks['full_decode'] and checks['has_audio'] and checks['black_events'] == []
        title = row['title'] + ' | Человек-паук'
        description = (row['title'] + '.\nЧеловек-паук / Spider-Man (2002), режиссёр Сэм Рэйми. '
                       'Русский дубляж.\nМузыка и звуковые эффекты — из фильма, '
                       'дополнительная музыка не добавлена.\n\n#ЧеловекПаук #SpiderMan #Shorts')
        entries.append(dict(key=row['key'], number=row['number'], file=str(file.resolve()),
                            title=title, description=description,
                            tags=['Человек-паук', 'Spider-Man', 'Тоби Магуайр', 'Shorts'],
                            sha256=digest, bytes=file.stat().st_size, duration=checks['duration'],
                            state='prepared'))
    data = dict(channel_handle='@kinomomentedit', publish_times_msk=['09:00', '13:00', '17:00'],
                authorization=dict(user_message=APPROVAL, recorded_at=datetime.now(timezone.utc).isoformat(),
                                   exact_files={e['key']: e['sha256'] for e in entries}), videos=entries)
    base.save(path, data)
    base.report(data)
    for row in reviewed:
        row['approval'] = 'approved'
        base.save(REVIEW / row['key'] / 'approval.json',
                  dict(state='approved', upload_authorized=True, sha256=row['sha256'], user_message=APPROVAL))
    base.save(REVIEW / 'manifest.json', reviewed)
    return data


def verify(uploader, data):
    ids = [e['youtube_id'] for e in data['videos'] if e.get('youtube_id')]
    remote = base.api(uploader, 'videos', part='snippet,status,processingDetails', id=','.join(ids))['items'] if ids else []
    by_id = {v['id']: v for v in remote}
    errors = []
    for e in data['videos']:
        v = by_id.get(e.get('youtube_id'))
        if not v:
            errors.append(e['key'] + ': not uploaded')
            continue
        assert v['snippet']['channelId'] == data['channel_id']
        status = v['status']
        when = status.get('publishAt')
        if (status.get('privacyStatus') != 'private' or not when
                or datetime.fromisoformat(when.replace('Z', '+00:00')) != datetime.fromisoformat(e['publish_at'])
                or status.get('uploadStatus') in {'failed', 'rejected', 'deleted'}
                or v.get('processingDetails', {}).get('processingStatus') == 'failed'):
            errors.append(e['key'] + ': unexpected schedule or processing status')
        else:
            e['state'] = 'scheduled_verified'
        e.update(server_status=status, processing=v.get('processingDetails', {}))
    data['verification_errors'] = errors
    base.save(OUT / 'manifest.json', data)
    base.report(data)
    base.save(OUT / 'server_verification.json', dict(checked_at=datetime.now(timezone.utc).isoformat(),
                                                   videos=remote, errors=errors))
    reviewed = base.read(REVIEW / 'manifest.json')
    for row, e in zip(reviewed, data['videos']):
        if e.get('youtube_id'):
            row.update(uploaded=True, youtube_id=e['youtube_id'], publish_at=e.get('publish_at'))
    base.save(REVIEW / 'manifest.json', reviewed)
    assert not errors, errors
    print('VERIFIED', len(remote), flush=True)


def upload(cfg, uploader, data):
    state = State(cfg.paths.path('state'))
    for e in data['videos']:
        if e.get('youtube_id'):
            continue
        assert e['state'] != 'uploading', 'Interrupted upload: reconcile remote inventory before retry'
        file = base.Path(e['file'])
        assert hashlib.file_digest(file.open('rb'), 'sha256').hexdigest() == data['authorization']['exact_files'][e['key']]
        when = datetime.fromisoformat(e['publish_at'])
        assert when > datetime.now(timezone.utc)
        base.vpn_check(cfg)
        e['state'] = 'uploading'
        base.save(OUT / 'manifest.json', data)
        try:
            yt = uploader.upload(file, base.VideoMeta(e['title'], e['description'], e['tags']), when)
        except base.QuotaExceeded:
            e['state'] = 'quota_wait'
            base.save(OUT / 'manifest.json', data)
            base.report(data)
            raise
        e.update(youtube_id=yt, state='uploaded_unverified')
        base.save(OUT / 'manifest.json', data)
        base.report(data)
        if not state.db.execute('SELECT 1 FROM videos WHERE youtube_id=?', (yt,)).fetchone():
            state.add_video(base.ROOT / 'library/Spider.Man.2002.OM.WEB-DL.AMZN.1080p-SOFCJ.mkv',
                            file, e['title'], yt, when)
        remote = base.api(uploader, 'videos', part='snippet,status,processingDetails', id=yt)['items'][0]
        assert remote['snippet']['channelId'] == data['channel_id']
        assert remote['status']['privacyStatus'] == 'private'
        assert datetime.fromisoformat(remote['status']['publishAt'].replace('Z', '+00:00')) == when
        e.update(state='scheduled_verified', server_status=remote['status'],
                 processing=remote.get('processingDetails', {}))
        base.save(OUT / 'manifest.json', data)
        base.report(data)
        print('SCHEDULED', yt, when.astimezone(base.MSK).isoformat(), flush=True)
    verify(uploader, data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'preflight', 'authorize-upload', 'upload', 'verify'])
    args = parser.parse_args()
    base.OUT = OUT
    data = prepare()
    if args.action == 'prepare':
        print('PREPARED', len(data['videos']), flush=True)
        return
    cfg = base.load_config()
    uploader = base.UploaderAgent(cfg)
    if args.action == 'authorize-upload':
        base.vpn_check(cfg)
        uploader.authorize()
    if args.action == 'verify':
        verify(uploader, data)
        return
    base.preflight(cfg, uploader, data)
    if args.action in {'upload', 'authorize-upload'}:
        upload(cfg, uploader, data)


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO)
    # The OAuth callback logger includes the authorization code in its URL.
    logging.getLogger('google_auth_oauthlib.flow').setLevel(logging.WARNING)
    main()
