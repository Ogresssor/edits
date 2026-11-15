"""Check selection edges against whole-scene speech; flag ASR uncertainty rather than hiding it."""
import json
from pathlib import Path
import spiderman_approved_workflow_batch as work

ROOT=work.film.ROOT
OUT=work.OUT/'integrity_review'
BASE={'S03':2306,'S06':3679,'S07':3765,'S09':4502,'S01':1602,'S10':4994}


def main():
    results=[]
    for key,p in work.PLANS.items():
        candidate=p['candidate']
        rows=json.loads((ROOT/'review/spiderman_full_review/new_candidates_audit'/candidate/'transcript.json').read_text(encoding='utf-8'))
        words=[dict(start=BASE[candidate]+w[0],end=BASE[candidate]+w[1],text=w[2])
            for r in rows if not any(s in r['text'].lower() for s in ['субтитр','продолжение следует']) for w in r['words']]
        edges=[]
        for i,(a,b,_) in enumerate(p['parts']):
            planned_end=b
            b=a+round((b-a)*work.film.FPS)/work.film.FPS
            entering=[w for w in words if w['start']<a<w['end']]
            leaving=[w for w in words if w['start']<b<w['end']]
            before=max((w for w in words if w['end']<=a),key=lambda w:w['end'],default=None)
            following=min((w for w in words if w['start']>=a),key=lambda w:w['start'],default=None)
            last=max((w for w in words if w['end']<=b and w['end']>=a),key=lambda w:w['end'],default=None)
            edges.append(dict(part=i,start=a,end=b,planned_end=planned_end,start_inside_word=entering,end_inside_word=leaving,
                previous_word=before,first_word=following,last_word=last,
                gap_after_previous=round(a-before['end'],3) if before else None,
                lead_to_next=round(following['start']-a,3) if following else None,
                end_padding=round(b-last['end'],3) if last else None))
        results.append(dict(key=key,parts=edges))
        print(key,json.dumps(edges,ensure_ascii=False),flush=True)
    OUT.mkdir(exist_ok=True)
    (OUT/'speech_edges.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
