"""Second editorial selection: eight complete scenes, no publication changes."""
import argparse
import json
import logging
import math
import re
import subprocess
import sys
import time
from pathlib import Path
import remaining_season as engine

batch=engine.batch
ROOT=batch.ROOT
OUT=ROOT/'review/s03_stories_batch2'
PLANS={
 '01_e02_peace_war':dict(episode=2,title='Томми объяснил, почему мир начинается с угрозы',
    contexts=[(1768,1818)],segments=[(1774.2,1817.1)],gamma=1.08),
 '02_e03_charlie_mother':dict(episode=3,title='Томми объясняет сыну, почему мама не вернётся',
    contexts=[(893,960)],segments=[(899.1,919.7),(937.7,956.75)],gamma=1.04),
 '03_e03_arthur_disobeys':dict(episode=3,title='Артур отказался выполнять приказ Томми',
    contexts=[(374,410),(2418,2442)],segments=[(381.4,407.0),(2421.2,2437.8)],gamma=1.10),
 '04_e03_becoming_father':dict(episode=3,title='Артур объявил семье, что станет отцом',
    contexts=[(2988,3055)],segments=[(2998.2,3028.3),(3028.9,3037.9),(3045.1,3052.7)],gamma=1.08),
 '05_e04_polly_no_regret':dict(episode=4,title='Полли пришла исповедаться. Но не в убийстве',
    contexts=[(332,406)],segments=[(333.8,346.2),(357.4,367.9),(375.1,393.9),(395.9,405.5)],gamma=1.12),
 '06_e04_women_strike':dict(episode=4,title='Женщины Шелби решили, что с них хватит',
    contexts=[(759,829)],segments=[(764.0,788.7),(794.0,805.4),(807.0,814.3),(817.6,828.1)],gamma=1.05),
 '07_e05_johnny_heaven':dict(episode=5,title='Джонни решил, что попал в рай. У Томми была работа',
    contexts=[(436,528)],segments=[(438.0,454.4),(456.2,469.7),(470.7,481.8),
        (507.1,513.5),(515.6,527.7)],gamma=1.05,crop_x_by_part={'1':410}),
 '08_e06_alfie_limits':dict(episode=6,title='Алфи спросил Томми, кто на самом деле перешёл черту',
    contexts=[(1385,1434),(1475,1498)],segments=[(1394.3,1433.2),(1478.6,1483.5),
        (1489.2,1496.0)],gamma=1.17),
}
for p in PLANS.values():
    p.update(music=engine.MUSIC,bed=-24,context=[p['contexts'][0][0],p['contexts'][-1][1]])


def select(key):
    engine.OUT=OUT
    engine.PLANS=PLANS
    engine.select(key)


def study(model):
    OUT.mkdir(exist_ok=True)
    for key,p in PLANS.items():
        select(key)
        folder=OUT/key
        folder.mkdir(exist_ok=True)
        if (folder/'context_transcript.json').exists():
            continue
        rows=[]
        for i,(a,b) in enumerate(p['contexts']):
            wav=folder/f'context_{i:02}.wav'
            batch.ffmpeg.run(['-ss',str(a),'-i',str(batch.SOURCE),'-t',str(b-a),
                '-map','0:a:0','-vn','-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],timeout=120)
            segs,_=model.transcribe(str(wav),language='ru',word_timestamps=True,vad_filter=False,
                beam_size=5,condition_on_previous_text=False)
            for s in segs:
                row=dict(start=round(a+s.start,3),end=round(a+s.end,3),text=s.text.strip(),
                    words=[[round(a+w.start,3),round(a+w.end,3),w.word.strip()] for w in s.words or []])
                rows.append(row)
                print(key,row['start'],row['end'],row['text'],flush=True)
            batch.ffmpeg.run(['-ss',str(a),'-i',str(batch.SOURCE),'-t',str(b-a),
                '-vf',f'fps=16/{b-a},scale=270:-2,tile=4x4','-frames:v','1',
                str(folder/f'context_sheet_{i:02}.jpg')],timeout=120)
        batch.save(folder/'context_transcript.json',rows)


def captions(key,model):
    import cv2
    folder=OUT/key
    path=folder/'timeline.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    backup=folder/'timeline_context_captions.json'
    if not backup.exists(): batch.save(backup,data)
    events=[]
    transcript=[]
    corrections=[]
    cached_path=folder/'clean_fragment_transcript.json'
    receipt=folder/'caption_cache_inputs.json'
    def signature():
        return [dict(file=c['file'],start=c['start'],dur=c['dur'],
            bytes=Path(c['file']).stat().st_size,mtime_ns=Path(c['file']).stat().st_mtime_ns)
            for c in data['clips']]
    valid=(cached_path.exists() and receipt.exists()
        and json.loads(receipt.read_text(encoding='utf-8'))==signature())
    cached=json.loads(cached_path.read_text(encoding='utf-8')) if valid else None
    for clip in data['clips']:
        cap=cv2.VideoCapture(clip['file'])
        frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); cap.release()
        assert frames>0
        clip['last_frame']=round((frames-1)/25,4)
        rows=[]
        if cached is None:
            segs,_=model.transcribe(clip['file'],language='ru',word_timestamps=True,vad_filter=False,
                beam_size=5,condition_on_previous_text=False)
            rows=[dict(start=s.start,end=s.end,text=s.text,
                words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segs]
        else:
            rows=[dict(start=r['start']-clip['start'],text=r['text'],
                words=[[w[0]-clip['start'],w[1]-clip['start'],w[2]] for w in r['words']])
                for r in cached if clip['start']-.001<=r['start']<clip['start']+clip['dur']-.001]
        for row in rows:
            transcript.append(dict(start=row['start']+clip['start'],text=row['text'],
                words=[[w[0]+clip['start'],w[1]+clip['start'],w[2]] for w in row['words']]))
        clean=[]
        for row in rows:
            text=row['text'].strip().lower().strip('.!?… ')
            if text in {'продолжение следует','не спросись'}:
                corrections.append(dict(at=clip['start']+row['start'],omitted=row['text'],
                    reason='ASR hallucination' if text=='продолжение следует' else 'uncertain overlapping speech'))
            else: clean.append(row)
        words=batch.fixed_words(clean)
        normalized=[]
        i=0
        names={'ченгрету':'Чангретту','ченгретту':'Чангретту','идден':'Иден','боллингу':'Булл-рингу'}
        while i<len(words):
            w=words[i]
            n=w.text.lower().strip('.,!?…:;')
            if n=='ченгрэ' and i+1<len(words) and words[i+1].text.lower().strip('.,!?')=='ты':
                normalized.append(batch.subtitles.Word(w.start,words[i+1].end,'Чангретты.'))
                corrections.append(dict(at=clip['start']+w.start,corrected='Ченгрэ. Ты → Чангретты'))
                i+=2; continue
            if n=='навстречу':
                split=min(w.start+.12,w.end-.01)
                normalized.extend([batch.subtitles.Word(w.start,split,'на'),
                    batch.subtitles.Word(split,w.end,'встречу'+w.text[len(n):])])
                corrections.append(dict(at=clip['start']+w.start,corrected='на встречу'))
            else:
                if n in names:
                    corrections.append(dict(at=clip['start']+w.start,corrected=names[n]))
                    w.text=names[n]+w.text[len(n):]
                normalized.append(w)
            i+=1
        words=normalized
        words=[w for w in words if 0<=w.start<w.end<=clip['dur']+.06]
        for ev in batch.captions(words,clip['dur']):
            ev['start']=round(ev['start']+clip['start'],3)
            ev['end']=round(min(ev['end']+clip['start'],clip['start']+clip['dur']),3)
            events.append(ev)
    data['subs']=events
    data['total']=round(math.floor(data['total']*25+.5)/25,2)
    data['clips'][-1]['dur']=round(data['total']-data['clips'][-1]['start'],3)
    for ev in events: ev['end']=min(ev['end'],data['total'])
    data['music']['levels']=[[0,-96],[data['total'],-96]]
    batch.save(path,data)
    batch.save(folder/'clean_fragment_transcript.json',transcript)
    batch.save(folder/'caption_corrections.json',corrections)
    batch.save(receipt,signature())
    print('CAPTIONS READY',key,len(events),flush=True)


def package():
    final=OUT/'FINAL'; final.mkdir(exist_ok=True)
    manifest=[]
    lines=['# Вторая партия: восемь новых историй','',
        'Новые сцены из третьего сезона. Сохранены шрифты, анимация подписей, формат и тихий микс Rain. '
        'Эта партия не загружалась на YouTube.','',
        '| № | Серия | Ролик | Секунды |','|---|---|---|---:|']
    for i,(key,p) in enumerate(PLANS.items(),1):
        f=OUT/key; latest=json.loads((f/'latest.json').read_text(encoding='utf-8'))
        c=json.loads((f/'checks.json').read_text(encoding='utf-8'))
        assert c['file']==latest['video'] and c['full_decode'] and c.get('black_events')==[]
        assert c['matched']>=.75*c['words'] and float(c['loudness']['input_tp'])<0
        dest=final/f'{key}.mp4'
        source=f/latest['video']
        if dest.exists() and not dest.samefile(source): dest.unlink()
        if not dest.exists(): dest.hardlink_to(source)
        relative=dest.relative_to(OUT).as_posix()
        lines.append(f"| {i} | {p['episode']} | [{p['title']}]({relative}) | {c['duration']:.1f} |")
        credit=json.loads((f/'music_credit.json').read_text(encoding='utf-8'))
        batch.save(f/'metadata_draft.json',dict(title=p['title']+' | Острые козырьки',
            description=f"{p['title']}.\nОстрые козырьки / Peaky Blinders. Сезон 3, серия {p['episode']}.\n\n"
            f"Музыка: {credit['title']} — {credit['creator']}.\nИсточник: {credit['foreign_landing_url']}\n"
            f"Лицензия: {credit['license_url']}\n\n#ОстрыеКозырьки #PeakyBlinders #Shorts",
            tags=['Острые козырьки','Peaky Blinders','Томми Шелби','Shorts']))
        manifest.append(dict(key=key,title=p['title'],episode=p['episode'],review_file=relative,**latest,checks=c))
    lines.extend(['','Проверены полное декодирование, размеры, длительность, аудио, пики, границы подписей '
        'и отсутствие чёрных вспышек. Просмотрены листы кадров. Слова распознаны отдельно по монтажным '
        'фрагментам до рендера и повторно по финальному миксу. Оставшиеся флаги ASR сохранены рядом. '
        'Полного прослушивания здесь не было.','',
        'Техническая проверка файла и успех обработки YouTube не означают отсутствия заявок Content ID. '
        'Укороченный монтаж сам по себе не предоставляет прав на сериал или музыку.','',
        'Проекты AEP и media сохранены в папке каждого ролика. Готовый MP4 содержит финальный '
        'микс после AE. Для правок укажи номер ролика и время.'])
    (OUT/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    playlist=['#EXTM3U']
    for x in manifest: playlist.extend([f"#EXTINF:{x['checks']['duration']},{x['title']}",x['review_file']])
    (OUT/'ALL_8.m3u8').write_text('\n'.join(playlist)+'\n',encoding='utf-8')
    batch.save(OUT/'manifest.json',manifest)
    print('PACKAGED',len(manifest),flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,stream=sys.stdout,format='%(asctime)s %(message)s')
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['study','prepare','captions','render','check','package'])
    parser.add_argument('--only',choices=list(PLANS))
    parser.add_argument('--wait-for-render',action='store_true')
    args=parser.parse_args()
    if args.action=='study': study(batch.whisper())
    elif args.action=='package': package()
    else:
        model=batch.whisper() if args.action in {'captions','check'} else None
        for key in ([args.only] if args.only else PLANS):
            select(key)
            if args.action=='check' and args.wait_for_render:
                folder=OUT/key
                deadline=time.monotonic()+1200
                while True:
                    path=folder/'latest.json'
                    if path.exists() and path.stat().st_mtime>=(folder/'timeline.json').stat().st_mtime:
                        latest=json.loads(path.read_text(encoding='utf-8'))
                        if latest['video'].endswith('_quiet.mp4') and (folder/latest['video']).exists():
                            break
                    if time.monotonic()>deadline: raise TimeoutError(key)
                    time.sleep(2)
                checked=folder/'checks.json'
                if checked.exists() and checked.stat().st_mtime>=path.stat().st_mtime:
                    report=json.loads(checked.read_text(encoding='utf-8'))
                    if report['file']==latest['video'] and report.get('black_events')==[]:
                        print('ALREADY CHECKED',key,flush=True)
                        continue
            if args.action=='prepare': batch.prepare(key)
            elif args.action=='captions': captions(key,model)
            elif args.action=='render':
                batch.render(key); engine.finalize_music(key)
            elif args.action=='check': engine.check_final(key,model)
