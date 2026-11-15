"""Independent short-window recognition of uncertain words before AE rendering."""
import json
import sys
import s04_season_edit as e

SPOTS={
 'P101':[(1515.5,1525.5)],'P103':[(2965.5,2970.7)],'P201':[(1661,1667.5),(1623.8,1630)],
 'P202':[(2460.5,2465),(2530,2538.5),(2553.5,2557.2)],
 'P203':[(3243.1,3255)],'P302':[(1383.8,1389.1)],'P303':[(2818,2824.5)],
 'P401':[(1753,1763),(1764.5,1776.5),(1810.5,1814.5)],'P402':[(1949,1957)],
 'P404':[(3245,3250)],'P501':[(822,826)],
 'P504':[(3066,3077.4),(3083.7,3090)],'P601':[(566.5,571.3)],
 'P602':[(2061,2065),(2083,2088)],'P603':[(2422,2429.5)]}

def main():
    model=e.b.whisper();lookup={c['id']:c for c in e.candidates()}
    for key,ranges in SPOTS.items():
        folder=e.OUT/key;dest=folder/'spotcheck.json'
        if dest.exists():continue
        results=[]
        for i,(a,z) in enumerate(ranges):
            wav=folder/'media'/f'spot_{i:02}.wav'
            e.b.ffmpeg.run(['-ss',str(a),'-i',lookup[key]['source'],'-t',str(z-a),'-map','0:a:0','-vn','-ar','16000','-ac','1',str(wav)],timeout=60)
            rows=e.first.recognize(model,wav,a);results+=rows
            print(key,a,' / '.join(r['text'] for r in rows),flush=True)
        e.b.save(dest,results)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace');main()
