"""Real FFmpeg integration check using generated media, not a movie."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from edits import media
from edits.analysis import analyze
from edits.core import Store, read, save
from edits.render import render, check


def main():
    root = Path(__file__).resolve().parents[1] / '.local' / 'smoke'
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'generated.mp4'; music = root / 'music.wav'
    media.run(['-y','-f','lavfi','-i','testsrc2=size=640x360:rate=25:duration=6',
               '-f','lavfi','-i','sine=frequency=440:duration=6','-c:v','libx264','-pix_fmt','yuv420p',
               '-c:a','aac','-shortest',str(source)])
    media.run(['-y','-f','lavfi','-i','sine=frequency=220:duration=6',str(music)])
    store = Store(root/'workspace'); scene_map = analyze(source, store)
    plan = dict(id='integration', source=str(source), scene_map=str(scene_map), audio_track=0,
                rating='strong', story={k:'Synthetic integration fixture' for k in
                 ('hook','development','payoff','context','selection_reason')},
                segments=[dict(start=.4,end=1.6),dict(start=3,end=4.8)],
                captions=[dict(start=.1,end=1.1,text='Проверка субтитров'),dict(start=1.3,end=2.8,text='Вторая реплика')],
                music=dict(path=str(music),creator='FFmpeg sine generator',title='Synthetic fixture',
                           license_url='https://example.invalid/test-only',source_url='generated locally'))
    path=root/'plan.json'; save(path,plan)
    file=render(path,store); before=file.stat().st_mtime_ns
    assert render(path,store)==file and file.stat().st_mtime_ns==before, 'Cache missed'
    report=check(file,store)
    assert report['passed'], report
    for profile in ('clean','accent','cinema'):
        render(path,store,profile,True)
    save(root/'result.json',dict(passed=True,qa=report,receipt=read(file.parent/'receipt.json'),
                                cache_reused=True,preview_profiles=['clean','accent','cinema']))
    print('REAL_RENDER_PASS',file)
    print('RENDER_SECONDS',read(file.parent/'receipt.json')['elapsed_seconds'])
    print('TRUE_PEAK',report['loudness']['input_tp'])


if __name__ == '__main__': main()
