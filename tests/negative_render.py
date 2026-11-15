"""Real media rejection checks: black video, silence, damaged export."""
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from edits import media
from edits.core import Store, save, sha256
from edits.render import check


def receipt(file):
    save(file.parent/'receipt.json',dict(preview=False,sha256=sha256(file),expected_duration=3,
         revision='negative-fixture',identity=dict(plan=dict(id='negative',segments=[dict(start=0,end=3)]),source_sha256='synthetic')))


def main():
    root=ROOT/'.local/negative-tests';store=Store(root/'workspace');results=[]
    for name,video,audio in (
        ('black','color=c=black:size=320x568:rate=25:duration=3','sine=frequency=440:duration=3'),
        ('silent','testsrc2=size=320x568:rate=25:duration=3','anullsrc=r=48000:cl=mono')):
        folder=root/name;folder.mkdir(parents=True,exist_ok=True);file=folder/'video.mp4'
        media.run(['-y','-f','lavfi','-i',video,'-f','lavfi','-i',audio,'-t','3',
                   '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(file)])
        receipt(file);report=check(file,store)
        assert not report['passed'],name+' unexpectedly passed'
        results.append(dict(test=name,rejected=True,report=report))
    damaged=root/'damaged';damaged.mkdir(exist_ok=True)
    file=damaged/'video.mp4';file.write_bytes(b'not a complete MP4 export')
    receipt(file)
    try:check(file,store)
    except RuntimeError:results.append(dict(test='damaged',rejected=True))
    else:raise AssertionError('Damaged export unexpectedly passed')
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM artifacts').fetchone()[0]==0
    save(root/'result.json',dict(passed=True,results=results))
    print('NEGATIVE_MEDIA_PASS',','.join(r['test'] for r in results))


if __name__=='__main__':main()
