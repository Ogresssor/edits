"""Resolve caption timing across long pauses with short source windows."""
import sys
import s04_season_edit as e
RANGES={'P102':[(1387.6,1390.6)],'P103':[(2847.9,2856)],
 'P203':[(3165,3179),(3180.4,3188.1),(3248,3254.8)],
 'P204':[(2346.8,2349.2)],'P302':[(1386.8,1389.2),(1422.8,1425)],
 'P303':[(2816.4,2820.2)]}

def main():
    model=e.b.whisper();lookup={c['id']:c for c in e.candidates()}
    for key,ranges in RANGES.items():
        folder=e.OUT/key;dest=folder/'timing_spots.json'
        if dest.exists():continue
        result=[]
        for i,(a,z) in enumerate(ranges):
            wav=folder/'media'/f'timing_spot_{i:02}.wav'
            e.b.ffmpeg.run(['-ss',str(a),'-i',lookup[key]['source'],'-t',str(z-a),'-map','0:a:0','-vn','-ar','16000','-ac','1',str(wav)],timeout=60)
            rows=e.first.recognize(model,wav,a);result+=rows
            print(key,a,[(round(w[0],2),w[2]) for r in rows for w in r['words']],flush=True)
        e.b.save(dest,result)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace');main()
