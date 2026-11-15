"""Inspect only unused strong candidates; never reads or changes YouTube."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import series_batch as batch

ROOT=batch.ROOT/'review/spiderman_full_review'
OUT=ROOT/'new_candidates_audit'
EXCLUDED={'S04','S05'}


def main():
    OUT.mkdir(exist_ok=True)
    candidates=[c for c in json.loads((ROOT/'candidates.json').read_text(encoding='utf-8'))
                if c['tier']=='strong' and c['id'] not in EXCLUDED]
    assert len(candidates)==10
    model=batch.whisper()
    for c in candidates:
        key=c['id'];source=ROOT/'previews'/f'{key}.mp4'
        folder=OUT/key;folder.mkdir(exist_ok=True)
        duration=batch.ffmpeg.probe(source).duration
        sheet=folder/'sheet.jpg'
        if not sheet.exists():
            batch.ffmpeg.run(['-i',str(source),'-vf',f'fps=24/{duration},scale=320:-2,tile=4x6',
                '-frames:v','1',str(sheet)],timeout=60)
        transcript=folder/'transcript.json'
        if not transcript.exists():
            segs,_=model.transcribe(str(source),language='ru',word_timestamps=True,vad_filter=False,
                beam_size=5,condition_on_previous_text=False)
            rows=[dict(start=s.start,end=s.end,text=s.text,
                words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segs]
            batch.save(transcript,rows)
        rows=json.loads(transcript.read_text(encoding='utf-8'))
        (folder/'dialogue.txt').write_text('\n'.join(
            f"{r['start']:.2f}–{r['end']:.2f}: {r['text']}" for r in rows),encoding='utf-8')
        print('AUDITED',key,duration,flush=True)


if __name__=='__main__':main()
