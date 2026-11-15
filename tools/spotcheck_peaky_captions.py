"""Recognize disputed caption windows in the actual rendered finals."""
import json
from pathlib import Path
import series_batch as b

ROOT=b.ROOT/'review/s03_stories_batch2'
OUT=ROOT/'review_2026-11-11'
WINDOWS={
 '02_e03_charlie_mother':[(20,28),(34,39.64)],
 '05_e04_polly_no_regret':[(27,36),(45,51.32)],
 '07_e05_johnny_heaven':[(40,45),(55,59.52)],
 '08_e06_alfie_limits':[(38,44)],
}

def main():
    model=b.whisper();results=[]
    for key,windows in WINDOWS.items():
        folder=ROOT/key;latest=json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        for a,z in windows:
            wav=OUT/f'{key}_{a}.wav'
            b.ffmpeg.run(['-ss',str(a),'-i',str(folder/latest['video']),'-t',str(z-a),'-vn',
                          '-ac','1','-ar','16000',str(wav)],timeout=60)
            segments,_=model.transcribe(str(wav),language='ru',word_timestamps=True,
                vad_filter=False,beam_size=5,condition_on_previous_text=False)
            rows=[dict(start=a+s.start,end=a+s.end,text=s.text,
                       words=[[a+w.start,a+w.end,w.word.strip()] for w in s.words or []]) for s in segments]
            results.append(dict(key=key,window=[a,z],transcript=rows))
            print(key,a,json.dumps(rows,ensure_ascii=False),flush=True)
    b.save(OUT/'caption_spotchecks.json',results)

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8');main()
