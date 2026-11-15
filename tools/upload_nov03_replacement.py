"""Upload one explicitly requested replacement into 3 November, 17:00 MSK."""
import hashlib
import logging
from datetime import datetime, timezone

import upload_approved_season as base


def main():
    logging.basicConfig(level=logging.INFO)
    base.OUT = base.ROOT / 'review/youtube_replacement_2026-11-03'
    base.OUT.mkdir(exist_ok=True)
    path = base.OUT / 'manifest.json'
    when = datetime.fromisoformat('2026-11-03T17:00:00+03:00')
    assert datetime.now(timezone.utc) < when, 'Requested slot has already passed'
    if path.exists():
        data = base.read(path)
    else:
        folder = base.ROOT / 'review/s03_stories_batch2/01_e02_peace_war'
        latest = base.read(folder / 'latest.json')
        file = folder / latest['video']
        meta = base.read(folder / 'metadata_draft.json')
        info = base.ffmpeg.probe(file)
        entry = dict(key='01_e02_peace_war', file=str(file.resolve()), **meta,
                     episode=2, duration=info.duration, bytes=file.stat().st_size,
                     sha256=hashlib.file_digest(file.open('rb'), 'sha256').hexdigest(),
                     state='prepared', publish_at=when.isoformat())
        data = dict(channel_handle='@kinomomentedit', videos=[entry],
                    replaces_blocked_video='bxE7rypoCOE')
        base.save(path, data)
    cfg = base.load_config()
    uploader = base.UploaderAgent(cfg)
    base.preflight(cfg, uploader, data)
    inventory = base.read(base.OUT / 'remote_inventory.json')
    for video in inventory['videos']:
        at = video.get('status', {}).get('publishAt')
        if at and datetime.fromisoformat(at.replace('Z', '+00:00')) == when:
            assert video['id'] in {'bxE7rypoCOE', data['videos'][0].get('youtube_id')}, 'Slot occupied by another video'
    # The existing uploader formats its input as UTC; always pass a UTC timestamp.
    data['videos'][0]['publish_at'] = when.astimezone(timezone.utc).isoformat()
    base.save(path, data)
    # This recovery path must never upload again: update the known server ID only.
    assert data['videos'][0].get('youtube_id') == '_AJT8R7hI40'
    entry = data['videos'][0]
    remote = base.api(uploader, 'videos', part='snippet,status,processingDetails', id=entry['youtube_id'])['items'][0]
    assert remote['snippet']['channelId'] == data['channel_id']
    if datetime.fromisoformat(remote['status']['publishAt'].replace('Z', '+00:00')) != when:
        response = uploader.session.put(base.API + 'videos', params={'part': 'status'},
            json={'id': entry['youtube_id'], 'status': {'privacyStatus': 'private',
                'publishAt': when.astimezone(timezone.utc).isoformat(), 'selfDeclaredMadeForKids': False}}, timeout=60)
        response.raise_for_status()
        remote = base.api(uploader, 'videos', part='snippet,status,processingDetails', id=entry['youtube_id'])['items'][0]
    assert datetime.fromisoformat(remote['status']['publishAt'].replace('Z', '+00:00')) == when
    entry.update(state='scheduled_verified', server_status=remote['status'], processing=remote.get('processingDetails', {}))
    base.save(path, data)
    base.report(data)
    base.save(base.OUT / 'server_verification.json', remote)
    print('REPLACEMENT VERIFIED', entry['youtube_id'], when.isoformat(), flush=True)


if __name__ == '__main__':
    main()
