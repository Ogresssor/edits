from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import media
from .core import canonical, read, save, sha256, validate_plan
from .analysis import require_coverage

PROFILES = {
    'clean': dict(font='Arial', size=58, primary='&H00FFFFFF', outline=3, margin=330),
    'accent': dict(font='Arial', size=62, primary='&H006CE8F3', outline=3, margin=330),
    'cinema': dict(font='Arial', size=54, primary='&H00EAEAEA', outline=2, margin=350),
}


def ass_time(value):
    centiseconds = round(value * 100)
    return f'{centiseconds//360000}:{centiseconds//6000%60:02}:{centiseconds//100%60:02}.{centiseconds%100:02}'


def captions(plan, profile, width, height):
    style = PROFILES[profile]
    scale = width / 1080
    header = (f'[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n'
              '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
              f"Style: Default,{style['font']},{round(style['size']*scale)},{style['primary']},&H00FFFFFF,&H00202020,&H80000000,-1,0,0,0,100,100,0,0,1,{style['outline']*scale},0,2,{round(90*scale)},{round(180*scale)},{round(style['margin']*scale)},1\n"
              '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    return header + ''.join(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Default,,0,0,0,,{c['text']}\n" for c in plan.get('captions', []))


def render(plan_path: Path, store, profile='clean', preview=False):
    plan = read(plan_path); duration = validate_plan(plan)
    source = Path(plan['source']).resolve()
    info = media.probe(source)
    if not info['has_audio'] or any(s['end'] > info['duration'] for s in plan['segments']):
        raise ValueError('Missing audio or segment outside source')
    audio_track = plan.get('audio_track', 0)
    if not isinstance(audio_track, int) or isinstance(audio_track, bool) or audio_track < 0:
        raise ValueError('audio_track must be a non-negative audio stream index')
    music = plan.get('music')
    source_digest = store.source_hash(source)
    require_coverage(Path(plan['scene_map']), source_digest, info['duration'])
    identity = dict(plan=plan, source_sha256=source_digest, profile=profile, preview=preview,
                    renderer='ffmpeg-v2.2', ffmpeg=media.executable())
    if music:
        identity['music_sha256'] = sha256(Path(music['path']))
    revision = canonical(identity)
    job = store.root / 'renders' / plan['id'] / revision
    job.mkdir(parents=True, exist_ok=True)
    file = job / 'video.mp4'; receipt = job / 'receipt.json'
    if file.exists() and receipt.exists() and read(receipt).get('sha256') == sha256(file):
        return file
    width, height = (540, 960) if preview else (1080, 1920)
    (job / 'captions.ass').write_text(captions(plan, profile, width, height), encoding='utf-8-sig')
    args = ['-y']; filters = []; links = []
    for index, segment in enumerate(plan['segments']):
        length = segment['end'] - segment['start']
        args += ['-ss', str(segment['start']), '-t', str(length), '-i', str(source)]
        if profile == 'cinema':
            filters += [f'[{index}:v:0]setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x101216,setsar=1[v{index}]']
        else:
            filters += [f'[{index}:v:0]setpts=PTS-STARTPTS,split=2[fgin{index}][bgin{index}]',
                        f'[bgin{index}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=12:1,scale={width}:{height},setsar=1[bg{index}]',
                        f'[fgin{index}]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg{index}]',
                        f'[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2[v{index}]']
        filters += [f'[{index}:a:{audio_track}]atrim=duration={length},asetpts=PTS-STARTPTS,aresample=48000[a{index}]']
        links.append(f'[v{index}][a{index}]')
    filters += [''.join(links) + f'concat=n={len(links)}:v=1:a=1[vjoin][voice]',
                '[vjoin]ass=captions.ass[vout]']
    if music:
        index = len(plan['segments'])
        args += ['-stream_loop', '-1', '-i', str(Path(music['path']).resolve())]
        filters += ['[voice]asplit=2[main][key]',
                    f'[{index}:a:0]atrim=duration={duration},asetpts=PTS-STARTPTS,volume=0.12[bed]',
                    '[bed][key]sidechaincompress=threshold=0.008:ratio=8:attack=10:release=250[duck]',
                    '[main][duck]amix=inputs=2:duration=first:normalize=0[mix]']
        voice = 'mix'
    else:
        voice = 'voice'
    filters += [f'[{voice}]loudnorm=I=-16:TP=-3:LRA=9,aresample=48000[aout]']
    args += ['-filter_complex', ';'.join(filters), '-map', '[vout]', '-map', '[aout]',
             '-c:v', 'libx264', '-preset', 'veryfast' if preview else 'fast', '-crf', '25' if preview else '18',
             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
             '-t', str(duration), 'rendering.mp4']
    started = time.monotonic()
    log = media.run(args, cwd=job)
    (job / 'render.log').write_text(log, encoding='utf-8')
    temporary = job / 'rendering.mp4'
    measured = media.probe(temporary)
    if abs(measured['duration'] - duration) > max(.15, 2/info['fps']):
        raise ValueError('Export duration mismatch')
    temporary.replace(file)
    save(receipt, dict(revision=revision, sha256=sha256(file), expected_duration=duration,
                       preview=preview, profile=profile, measured=measured, elapsed_seconds=round(time.monotonic()-started, 2), identity=identity))
    return file


def check(file: Path, store):
    receipt = read(file.parent / 'receipt.json')
    if receipt['preview']:
        raise ValueError('Preview may not be approved for publication')
    digest = sha256(file)
    if digest != receipt['sha256']:
        raise ValueError('Export changed since rendering')
    started = time.monotonic()
    log = media.run(['-xerror', '-err_detect', 'explode', '-i', str(file.resolve()), '-vf', 'blackdetect=d=0.2:pix_th=0.1,freezedetect=n=-60dB:d=2',
                     '-af', 'loudnorm=I=-16:TP=-3:LRA=9:print_format=json', '-f', 'null', '-'])
    (file.parent / 'qa.log').write_text(log, encoding='utf-8')
    metrics = re.findall(r'\{\s*"input_i".*?\}', log, re.S)
    loudness = json.loads(metrics[-1]) if metrics else {}
    black = re.findall(r'black_start:[^\n]+', log)
    freeze = re.findall(r'freeze_start:[^\n]+', log)
    measured = media.probe(file)
    passed = (bool(loudness) and -35 <= float(loudness['input_i']) <= -5
              and float(loudness['input_tp']) <= 0 and not black and not freeze
              and abs(measured['duration']-receipt['expected_duration']) <= .15)
    report = dict(sha256=digest, passed=passed, full_decode=True, black_events=black, freeze_events=freeze,
                  loudness=loudness, elapsed_seconds=round(time.monotonic()-started,2),
                  human_video_audio_review=False,
                  warnings=['Longer than 60 seconds: check actual YouTube rights restrictions'] if measured['duration'] > 60 else [])
    save(file.parent / 'qa.json', report)
    if passed:
        story_key = canonical(dict(source=receipt['identity']['source_sha256'],
                                    segments=receipt['identity']['plan']['segments']))
        store.register(file, story_key, receipt['revision'], report)
    return report
