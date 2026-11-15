"""Audio-only alternatives; the approved video and captions stay byte-identical."""
import json
import subprocess
from series_batch import OUT, ROOT, ffmpeg, save


def main():
    folder = OUT / '01_grace_polly'
    latest = json.loads((folder/'latest.json').read_text(encoding='utf-8'))
    video = folder/latest['video']
    clean = folder/'clean_cut.mp4'
    duration = ffmpeg.probe(video).duration
    track_meta = next((ROOT/'music/auto').glob('jamendo_b6bccae4*.json'))
    meta = json.loads(track_meta.read_text(encoding='utf-8'))
    track = next(p for p in track_meta.parent.glob(track_meta.stem+'.*')
                 if p.suffix in {'.mp3', '.ogg', '.wav', '.m4a'})
    records = []
    for name in ['soft_music', 'original_sound']:
        dest = folder/f'01_grace_polly_{name}_v3.mp4'
        if dest.exists():
            raise FileExistsError(dest)
        args = ['-i', str(video), '-i', str(clean)]
        base = f'[1:a]aresample=48000,apad,atrim=duration={duration},asetpts=PTS-STARTPTS'
        if name == 'soft_music':
            args += ['-i', str(track)]
            graph = (base+' ,asplit=2[voice][key];'
                f'[2:a]aresample=48000,atrim=duration={duration},asetpts=PTS-STARTPTS,'
                'loudnorm=I=-16:TP=-3:LRA=9,aresample=48000,volume=-24dB,'
                f'afade=t=in:d=1,afade=t=out:st={duration-1}:d=1[bed];'
                '[bed][key]sidechaincompress=threshold=0.035:ratio=5:attack=15:release=350[ducked];'
                '[voice][ducked]amix=inputs=2:duration=first:normalize=0,'
                'loudnorm=I=-16:TP=-3:LRA=9,aresample=48000,alimiter=limit=0.7:level=false:latency=true,volume=-3dB[a]')
        else:
            graph = base+',loudnorm=I=-16:TP=-3:LRA=9,aresample=48000,alimiter=limit=0.7:level=false:latency=true,volume=-3dB[a]'
        ffmpeg.run(args+['-filter_complex', graph, '-map', '0:v:0', '-map', '[a]',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', str(duration),
            '-movflags', '+faststart', str(dest)], timeout=180)
        ffmpeg.run(['-i', str(dest), '-f', 'null', '-'], timeout=180)
        info = ffmpeg.probe(dest)
        assert info.has_audio and abs(info.duration-duration)<.1
        def video_hash(path):
            p = subprocess.run([ffmpeg.ffmpeg_exe(), '-v', 'error', '-i', str(path),
                '-map', '0:v:0', '-c:v', 'copy', '-f', 'hash', '-hash', 'sha256', '-'],
                capture_output=True, text=True, check=True)
            return p.stdout.strip()
        assert video_hash(video) == video_hash(dest)
        p = subprocess.run([ffmpeg.ffmpeg_exe(), '-hide_banner', '-i', str(dest),
            '-af', 'loudnorm=print_format=json', '-f', 'null', '-'],
            capture_output=True, text=True, encoding='utf-8', check=True)
        loudness = json.loads(p.stderr[p.stderr.rfind('{'):p.stderr.rfind('}')+1])
        assert float(loudness['input_tp'])<0
        records.append(dict(file=dest.name, duration=info.duration, full_decode=True,
            video_stream_unchanged=True, loudness=loudness,
            added_music=meta if name=='soft_music' else None))
        print('READY', dest, flush=True)
    save(folder/'music_alternatives.json', records)
    (folder/'MUSIC_REVIEW.md').write_text(
        '# Замена музыки\n\nПредыдущая версия сохранена. latest.json не переключён до выбора варианта.\n\n'
        '- [Тихая подложка](01_grace_polly_soft_music_v3.mp4): Rain — VolexOne, '
        'на 24 dB тише нормализованной речи, дополнительно приглушается речью.\n'
        '- [Оригинальный звук сцены](01_grace_polly_original_sound_v3.mp4): без добавленного трека; '
        'вся музыка и атмосфера самого сериала остаются.\n\n'
        'Видео и субтитры скопированы без перекодирования, совпадение потока проверено SHA-256. '
        'Обе версии полностью декодированы, проверены длительность и пики. '
        'Эмоциональное соответствие новой музыки прослушиванием здесь не подтверждено.\n\n'
        f"Rain: {meta['foreign_landing_url']} — {meta['license_url']}\n",
        encoding='utf-8')


if __name__ == '__main__':
    main()

