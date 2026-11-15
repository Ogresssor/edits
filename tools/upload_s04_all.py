"""Upload the remaining final S04 exports explicitly authorized by the user."""
import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import prioritize_peaky_backlog as p

b = p.b
OUT = b.ROOT / 'review/youtube_s04_all_2026-11-12'
SOURCE = b.ROOT / 'review/s04_season'
APPROVAL = 'так заливай все видосы\nчтобы в отложке они лежали'


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare():
    OUT.mkdir(exist_ok=True)
    path = OUT / 'manifest.json'
    if path.exists():
        return b.read(path)
    rows = b.read(SOURCE / 'manifest.json')
    pair = b.read(b.ROOT / 'review/youtube_s04_nov14_pair/manifest.json')
    excluded = {v['key'] for v in pair['videos'] if v.get('youtube_id')}
    assert excluded == {'P303', 'P403'}
    first = b.ROOT / 'review/s04_first/P301_polly_conditions'
    first_review = b.read(first / 'review_manifest.json')
    variant = next(v for v in first_review['variants'] if v['file'] == b.read(first / 'variants.json')['preferred'])
    rows.append(dict(key='P301', episode=3, title=first_review['title'],
                     video=str(first / variant['file']), sha256=variant['sha256'],
                     seconds=variant['duration'], editorial_note='Полли готова вернуться в компанию, но ставит Томми свои условия.'))
    videos = []
    for row in sorted(rows, key=lambda x: x['key']):
        if row['key'] in excluded:
            continue
        folder = first if row['key'] == 'P301' else SOURCE / row['key']
        file = Path(row['video']) if row['key'] == 'P301' else SOURCE / row['video']
        assert file.is_file() and digest(file) == row['sha256']
        check = variant if row['key'] == 'P301' else b.read(folder / 'integrity.json')
        assert check['sha256'] == row['sha256'] and check['full_decode']
        assert not check['black_events'] and not check['freeze_flags']
        credit = b.read(folder / 'music_credit.json')
        desc = (row['editorial_note'] + f"\nОстрые козырьки / Peaky Blinders. Сезон 4, серия {row['episode']}.\n\n"
                f"Музыка: {credit['title']} — {credit['creator']}.\nИсточник: {credit['foreign_landing_url']}\n"
                f"Лицензия: {credit['license_url']}\n\n#ОстрыеКозырьки #PeakyBlinders #Shorts")
        videos.append(dict(key=row['key'], file=str(file.resolve()), sha256=row['sha256'],
                           title=row['title'] + ' | Острые козырьки', description=desc,
                           tags=['Острые козырьки', 'Peaky Blinders', 'Shorts'],
                           duration=row['seconds'], state='approved_waiting_upload'))
    assert len(videos) == 19 and len({v['sha256'] for v in videos}) == 19
    data = dict(channel_id=pair['channel_id'], channel_handle=pair['channel_handle'], videos=videos,
                authorization=dict(user_message=APPROVAL, confirmed=True,
                                   files={v['key']:v['sha256'] for v in videos}),
                excluded_existing=pair['videos'])
    b.save(path, data)
    sync(data)
    return data


def sync(data):
    for v in data['videos']:
        folder = b.ROOT / 'review/s04_first/P301_polly_conditions' if v['key'] == 'P301' else SOURCE / v['key']
        b.save(folder / 'approval.json', dict(state='approved_for_scheduled_upload',
               upload_authorized=True, user_message=APPROVAL, sha256=v['sha256'],
               upload_state=v['state'], publish_at=v.get('publish_at'), youtube_id=v.get('youtube_id')))
    catalog = b.read(SOURCE / 'manifest.json')
    bykey = {v['key']:v for v in data['videos']}
    for row in catalog:
        if row['key'] in bykey:
            v = bykey[row['key']]
            row.update(approval='approved_for_scheduled_upload', upload_authorized=True,
                       upload_state=v['state'], uploaded=bool(v.get('youtube_id')))
            for key in ['publish_at', 'youtube_id']:
                if v.get(key): row[key] = v[key]
    b.save(SOURCE / 'manifest.json', catalog)
    b.OUT = OUT
    b.report(data)


def run(data):
    assert data['authorization']['confirmed']
    assert data['authorization']['files'] == {v['key']:v['sha256'] for v in data['videos']}
    cfg = b.load_config(); u = b.UploaderAgent(cfg)
    b.vpn_check(cfg); p.OUT = OUT
    channel, remote = p.inventory(u)
    assert channel == data['channel_id']
    known = {v.get('youtube_id') for v in data['videos'] if v.get('youtube_id')}
    occupied = [datetime.fromisoformat(v['status']['publishAt'].replace('Z','+00:00'))
                for v in remote if v['status'].get('publishAt') and v['id'] not in known]
    # Preserve the previously reserved Arthur slot while its separate retry is pending.
    old = b.read(b.ROOT / 'review/youtube_peaky_priority_2026-11-11/manifest.json')
    occupied += [datetime.fromisoformat(v['publish_at']) for v in old['videos'] if not v.get('youtube_id')]
    after = datetime.now(timezone.utc) + timedelta(minutes=30)
    start = datetime.now(b.MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    for v in data['videos']:
        if v.get('youtube_id'): continue
        assert v['state'] != 'uploading', 'Interrupted upload: reconcile server before retry'
        assert not any(x['snippet']['title'] == v['title'] for x in remote), 'Duplicate title on server'
        assert digest(Path(v['file'])) == v['sha256']
        when = datetime.fromisoformat(v['publish_at']) if v.get('publish_at') else None
        if when:
            assert when > after, 'Reserved slot has passed; review scheduling'
            assert all(abs((when - x).total_seconds()) >= 3600 for x in occupied), 'Reserved slot occupied'
        else:
            when = next((start + timedelta(days=d)).replace(hour=h) for d in range(90) for h in [9,13,17]
                        if (start + timedelta(days=d)).replace(hour=h) > after
                        and all(abs(((start + timedelta(days=d)).replace(hour=h) - x).total_seconds()) >= 3600 for x in occupied))
            v['publish_at'] = when.isoformat()
        occupied.append(when)
    b.save(OUT / 'manifest.json', data); sync(data)
    print('PLAN', len(data['videos']), data['videos'][0]['publish_at'], data['videos'][-1]['publish_at'], flush=True)
    for v in data['videos']:
        if not v.get('youtube_id'):
            b.vpn_check(cfg)
            v['state'] = 'uploading'; b.save(OUT / 'manifest.json', data)
            try:
                v['youtube_id'] = u.upload(Path(v['file']), b.VideoMeta(v['title'],v['description'],v['tags']), datetime.fromisoformat(v['publish_at']))
            except b.QuotaExceeded:
                v['state'] = 'quota_wait'; b.save(OUT / 'manifest.json', data); sync(data)
                print('QUOTA_WAIT', v['key'], flush=True)
                return 75
            v['state'] = 'uploaded_unverified'; b.save(OUT / 'manifest.json', data)
        server = p.verify_one(u, channel, v['youtube_id'], v['publish_at'])
        v.update(state='scheduled_verified', server_status=server['status'], processing=server.get('processingDetails',{}))
        b.save(OUT / 'manifest.json', data); sync(data)
        print('SCHEDULED', v['key'], v['youtube_id'], v['publish_at'], flush=True)
    p.inventory(u)
    print('COMPLETE', len(data['videos']), flush=True)
    return 0


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['prepare','run'])
    args = parser.parse_args(); data = prepare()
    if args.action == 'run': raise SystemExit(run(data))
    print('PREPARED', len(data['videos']))
