"""Уточнить субтитры пилота по независимому распознаванию чистых фрагментов."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot import subtitles

OUT=ROOT/'review/s03e01'
data=json.loads((OUT/'timeline.json').read_text(encoding='utf-8'))
words=subtitles._join_hyphens([subtitles.Word(*w) for w in json.loads(
    (OUT/'precise_words.json').read_text(encoding='utf-8'))])
cuts=[0,3.28,5.5,6.36,8.1,10.70,12.5,15.85,19.36,23.20,25.2,
      29,32.85,34.6,36.5,40.65,43.0,45.64]
events=[]
palette=['#FFE14D','#35F2FF','#FF4A3D','#7CFF5B','#FF4FD8']
for a,b in zip(cuts,cuts[1:]):
    ws=[w for w in words if a<=w.start<b]
    if not ws:
        continue
    top,main=subtitles._tiers(ws)
    start=ws[0].start
    tag=lambda seq:[dict(t=round(w.start-start,3),text=w.text.upper()) for w in seq]
    events.append(dict(start=round(start,3),end=round(min(data['total'],max(start+.5,ws[-1].end+.3)),3),
                       color=palette[len(events)%len(palette)],top=tag(top),main=tag(main)))
for ev,nxt in zip(events,events[1:]):
    ev['end']=round(min(ev['end'],nxt['start']+.08),3)
data['subs']=events
(OUT/'timeline_v1.json').write_text((OUT/'timeline.json').read_text(encoding='utf-8'),encoding='utf-8')
(OUT/'timeline.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
for e in events:
    print(e['start'],e['end'],' '.join(w['text'] for w in e['top']+e['main']))
