"""Редакторская партия S03E01. Изолирована от очереди публикаций.

study — точная расшифровка выбранных сцен; prepare — чистый монтаж и субтитры;
render — After Effects; check — проверка готовых файлов.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot import ffmpeg,reframe,subtitles,qa
from editbot.config import load_config
import numpy as np

OUT=ROOT/'review/s03e01_batch'
SOURCE=ROOT/'library/Peaky.Blinders.S03.1080p.BluRay/Peaky.Blinders.S03E01.1080p.BluRay.4xRus.Eng.Subs.mkv'
SCENES={
 '01_grace_polly':dict(title='Грейс раскрыла секрет — Полли узнала больше',context=[1932.8,2006.6],
     segments=[(1932.80,1953.20),(1970.52,1977.76),(1982.20,1986.88),(1988.80,1992.00),(1995.40,2006.48)],
     music='jamendo_10b8b566',gamma=1.0,bed=-19),
 '02_brothers':dict(title='Для них семья — слабость. Для Томми — сила',context=[1336.8,1439.4],
     segments=[(1336.80,1359.96),(1368.32,1372.28),(1392.20,1399.56),(1404.64,1412.40),(1417.40,1439.40)],
     music='jamendo_b6bccae4',gamma=1.12,bed=-20),
 '03_our_city':dict(title='Кто встретит деньги? Полиция. Это наш город',context=[1667.9,1708.8],
     segments=[(1668.20,1708.80)],music='wikimedia_audio_24d04bd1',gamma=1.15,bed=-19),
}


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def whisper():
    import torch
    lib=Path(torch.__file__).parent/'lib'
    os.environ['PATH']=str(lib)+os.pathsep+os.environ.get('PATH','')
    global DLL
    DLL=os.add_dll_directory(str(lib))
    from faster_whisper import WhisperModel
    return WhisperModel('large-v3',device='cuda',compute_type='float16',local_files_only=True)


def study():
    OUT.mkdir(parents=True,exist_ok=True)
    model=whisper()
    for key,plan in SCENES.items():
        folder=OUT/key
        folder.mkdir(exist_ok=True)
        a,b=plan['context']
        wav=folder/'context.wav'
        ffmpeg.run(['-ss',str(a),'-i',str(SOURCE),'-t',str(b-a),'-map','0:a:0','-vn',
                    '-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],timeout=90)
        segs,_=model.transcribe(str(wav),language='ru',word_timestamps=True,vad_filter=False,
                              beam_size=5,condition_on_previous_text=False)
        rows=[]
        for s in segs:
            row=dict(start=round(a+s.start,3),end=round(a+s.end,3),text=s.text.strip(),
                words=[[round(a+w.start,3),round(a+w.end,3),w.word.strip()] for w in s.words or []])
            rows.append(row)
            print(key,row['start'],row['end'],row['text'],flush=True)
        save(folder/'context_transcript.json',rows)
        ffmpeg.run(['-ss',str(a),'-i',str(SOURCE),'-t',str(b-a),'-vf',
            f'fps=16/{b-a},scale=270:-2,tile=4x4','-frames:v','1',str(folder/'context_sheet.jpg')],timeout=90)


def fixed_words(rows):
    import re
    raw=[]
    for row in rows:
        phrase=[subtitles.Word(*w) for w in row['words']]
        if phrase and not re.search(r'[.,!?…:;]$',phrase[-1].text):
            phrase[-1].text+='.'
        raw.extend(phrase)
    ws=subtitles._join_hyphens(raw)
    out=[]
    i=0
    while i<len(ws):
        w=ws[i]
        if i+1<len(ws) and w.text.lower()=='за' and ws[i+1].text.lower().startswith('мнем'):
            out.append(subtitles.Word(w.start,ws[i+1].end,'Замнём.'))
            i+=2
            continue
        # Не печатать сомнительно распознанное прилагательное как достоверную реплику.
        if w.text.lower().strip(',.?!') in {'драного','задранного'}:
            i+=1
            continue
        out.append(w)
        i+=1
    return out


def captions(words,duration):
    import re
    stop={'в','на','и','но','а','от','за','к','у','с','по','о','об','для','не','как','что','чтобы','же',
          'так','потому','только','десять','десятью'}
    chunks=[]
    start=0
    while start<len(words):
        end=start
        while end<len(words) and end-start<7:
            if end>start and len(' '.join(w.text for w in words[start:end+1]))>40:
                break
            end+=1
            if re.search(r'[.!?…]$',words[end-1].text):
                break
            if end<len(words) and words[end].start-words[end-1].end>.8:
                if words[end-1].text.lower().strip(',.!?') not in stop:
                    break
        end=max(start+1,end)
        while end>start+2 and end<len(words) and words[end-1].text.lower().strip(',.!?') in stop:
            end-=1
        chunks.append(words[start:end])
        start=end
    events=[]
    colors=['#FFE14D','#35F2FF','#FF4A3D','#7CFF5B','#FF4FD8']
    for chunk in chunks:
        a=chunk[0].start
        top,main=subtitles._tiers(chunk)
        tag=lambda seq:[dict(t=round(w.start-a,3),text=w.text.upper()) for w in seq]
        events.append(dict(start=round(a,3),end=round(min(duration,max(a+.55,chunk[-1].end+.30)),3),
            top=tag(top),main=tag(main),color=colors[len(events)%len(colors)]))
    for ev,nxt in zip(events,events[1:]):
        ev['end']=round(min(ev['end'],nxt['start']+.08),3)
    return events


def prepare(key):
    plan=SCENES[key]
    folder=OUT/key
    media=folder/'media'
    media.mkdir(parents=True,exist_ok=True)
    rows=json.loads((folder/'context_transcript.json').read_text(encoding='utf-8'))
    ws=fixed_words(rows)
    all_events,clips,mapped=[],[],[]
    offset=0.0
    for i,(a,b) in enumerate(plan['segments']):
        duration=round(b-a,2)
        recipe=dict(source=str(SOURCE),start=a,end=b,crop=1100,gamma=plan['gamma'],framing_version=2)
        crop_override=plan.get('crop_x_by_part',{}).get(str(i))
        crop_ranges=plan.get('crop_x_ranges',{}).get(str(i),[])
        if crop_override is not None:
            recipe['crop_x_override']=crop_override
        if crop_ranges:
            recipe['crop_x_ranges']=crop_ranges
        dest=media/f'part_{i:02}.mov'
        receipt=dest.with_suffix('.recipe.json')
        if not (dest.exists() and receipt.exists() and json.loads(receipt.read_text(encoding='utf-8'))==recipe):
            p=subprocess.run([ffmpeg.ffmpeg_exe(),'-v','error','-ss',str(a),'-i',str(SOURCE),'-t',str(duration),
                '-vf','scale=480:260','-pix_fmt','bgr24','-f','rawvideo','-'],capture_output=True,timeout=120)
            if p.returncode:
                raise RuntimeError(p.stderr.decode('utf-8','replace')[-1000:])
            frames=np.frombuffer(p.stdout,np.uint8).reshape(-1,260,480,3)
            centers,cuts=reframe.analyze_frames(frames,25)
            # Гистограммы двух ночных крупных планов почти одинаковы, хотя персонажи
            # меняются местами. Добавляем точные границы по резкому скачку пикселей.
            gray=frames.mean(axis=3).astype(np.float32)
            diffs=np.abs(gray[1:]-gray[:-1]).mean(axis=(1,2))
            last=-100
            for n,delta in enumerate(diffs):
                background=float(np.median(diffs[max(0,n-12):n+13]))
                if delta>max(9.0,background*4+2) and n-last>=7:
                    cuts[n+1]=True
                    last=n
            path=reframe.plan_path(centers,cuts,25,1100/1920/2,'smart')
            xs=np.clip(path*1920-550,0,820)
            if crop_override is not None:
                xs[:]=float(crop_override)
            for start,end,x in crop_ranges:
                xs[round(start*25):round(end*25)]=float(x)
            cmd=media/f'framing_{i:02}.txt'
            cmd.write_text('\n'.join(f'{n/25:.3f} crop@framing x {int(round(x/2))*2};' for n,x in enumerate(xs)),encoding='utf-8')
            vf=(f'sendcmd=f={cmd.name},crop@framing=1100:1040:410:0,scale=1080:1020:flags=lanczos,'
                f'pad=1080:1200:0:90:black,setsar=1,eq=contrast=1.03:saturation=1.03:gamma={plan["gamma"]}')
            ffmpeg.run(['-ss',str(a),'-i',str(SOURCE),'-t',str(duration),'-map','0:v:0','-map','0:a:0',
                '-vf',vf,'-c:v','libx264','-crf','16','-preset','slow','-pix_fmt','yuv420p',
                '-af','loudnorm=I=-16:TP=-2:LRA=9,aresample=48000','-ac','2','-c:a','pcm_s16le',str(dest)],cwd=media,timeout=300)
            save(receipt,recipe)
        # Переносим только целые слова; длительность речи не привязывается к биту.
        local=[subtitles.Word(round(w.start-a,3),round(w.end-a,3),w.text) for w in ws if a<=w.start and w.end<=b]
        evs=captions(local,duration)
        for ev in evs:
            ev['start']=round(ev['start']+offset,3)
            ev['end']=round(min(offset+duration,ev['end']+offset),3)
            all_events.append(ev)
        mapped.extend([w.start+offset,w.end+offset,w.text] for w in local)
        clips.append(dict(file=dest.resolve().as_posix(),start=offset,dur=duration,
            name=f'{a:.2f}–{b:.2f}',voice=True,fx={}))
        offset=round(offset+duration,2)
        print('PREPARED',key,i,duration,flush=True)
    meta_file=next((ROOT/'music/auto').glob(plan['music']+'*.json'))
    meta=json.loads(meta_file.read_text(encoding='utf-8'))
    track=next(p for p in meta_file.parent.glob(meta_file.stem+'.*') if p.suffix in {'.mp3','.ogg','.wav','.m4a'})
    music=media/'music.wav'
    ffmpeg.run(['-i',str(track),'-t',str(offset),'-af','loudnorm=I=-16:TP=-2:LRA=9','-ar','48000',
        '-ac','2','-c:a','pcm_s16le',str(music)],timeout=120)
    # Последнюю реплику не перекрываем подъёмом музыки.
    voice_end=max((w[1] for w in mapped),default=offset)
    levels=[[0,-35],[.5,plan['bed']]]
    if offset-voice_end>.6:
        levels.extend([[round(voice_end+.08,2),plan['bed']],[round(min(offset-.25,voice_end+.35),2),-10]])
    else:
        levels.append([round(max(.51,offset-.3),2),plan['bed']])
    levels.append([offset,-40])
    data=dict(name=plan['title'],w=1080,h=1200,fps=25,total=offset,clips=clips,flashes=[],plate=None,
        music=dict(file=music.resolve().as_posix(),levels=levels),subs=all_events,
        sub_style=dict(top_font='Montserrat-SemiBold',main_font='Montserrat-ExtraBoldItalic',top_size=42,
            main_size=76,y_pct=56,top_tracking=120,main_tracking=20,glow_threshold=90,glow_radius=28,glow_intensity=1.3))
    save(folder/'timeline.json',data)
    save(folder/'mapped_words.json',mapped)
    save(folder/'plan.json',dict(plan,source=str(SOURCE),duration=offset))
    save(folder/'music_credit.json',{k:meta.get(k) for k in ['title','creator','license','license_url','foreign_landing_url']})
    concat=media/'concat.txt'
    concat.write_text('\n'.join(f"file 'part_{i:02}.mov'" for i in range(len(clips))),encoding='utf-8')
    ffmpeg.run(['-f','concat','-safe','0','-i',str(concat),'-c:v','copy','-c:a','aac','-b:a','192k',
        '-movflags','+faststart',str(folder/'clean_cut.mp4')],timeout=120)
    ffmpeg.run(['-i',str(folder/'clean_cut.mp4'),'-vf',f'fps=16/{offset},scale=270:-2,tile=4x4',
        '-frames:v','1',str(folder/'framing_sheet.jpg')],timeout=120)
    print('READY FOR REVIEW',key,offset,'seconds',len(all_events),'captions',flush=True)


def render(key):
    from editbot.ae.bridge import AfterEffects,find_ae
    folder=OUT/key
    data=json.loads((folder/'timeline.json').read_text(encoding='utf-8'))
    stem=key
    revision=1
    while any(p.exists() for p in (folder/(stem+'.mp4'), folder/(stem+'.aep'), folder/'media'/(stem+'_ae.mp4'))):
        revision+=1
        stem=f'{key}_v{revision}'
    target=folder/(stem+'.mp4')
    raw=folder/'media'/(stem+'_ae.mp4')
    data['render_to']=raw.resolve().as_posix()
    ae=AfterEffects(find_ae(),Path('C:/aebridge'),20,35)
    project=ae.build(data,folder/(stem+'.aep'))
    ae.render(project,raw,expected_duration=data['total'])
    ffmpeg.run(['-i',str(raw),'-c:v','copy','-af','loudnorm=I=-16:TP=-3:LRA=9','-c:a','aac','-b:a','192k',
        '-movflags','+faststart',str(target)],timeout=120)
    save(folder/'latest.json',dict(video=target.name,project=project.name,revision=revision))
    print('RENDERED',target,flush=True)


def check(key,model):
    import re
    folder=OUT/key
    latest=json.loads((folder/'latest.json').read_text(encoding='utf-8'))
    video=folder/latest['video']
    data=json.loads((folder/'timeline.json').read_text(encoding='utf-8'))
    info=ffmpeg.probe(video)
    assert info.has_audio and (info.width,info.height)==(1080,1200)
    assert abs(info.duration-data['total'])<.15
    assert all(0<=e['start']<e['end']<=data['total'] for e in data['subs'])
    ffmpeg.run(['-i',str(video),'-f','null','-'],timeout=180)
    transcript_file=folder/'rendered_transcript.json'
    if transcript_file.exists() and transcript_file.stat().st_mtime>=video.stat().st_mtime:
        rows=json.loads(transcript_file.read_text(encoding='utf-8'))
    else:
        segs,_=model.transcribe(str(video),language='ru',word_timestamps=True,vad_filter=False,beam_size=5,
            condition_on_previous_text=False)
        rows=[dict(start=s.start,end=s.end,text=s.text,words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segs]
        save(transcript_file,rows)
    heard=[(w[0],qa._norm(w[2])) for s in rows for w in s['words']]
    offsets,flags=[],[]
    total=0
    for ev in data['subs']:
        for w in ev['top']+ev['main']:
            n=qa._norm(w['text'])
            if len(n)<3:
                continue
            total+=1
            t=ev['start']+w['t']
            h=qa._heard_at(heard,t,n)
            if h is not None: offsets.append(h-t)
            if h is None or abs(h-t)>.3:
                flags.append(dict(at=round(t,3),text=w['text'],heard=h,delta=round(h-t,3) if h is not None else None))
    save(folder/'sync_flags.json',flags)
    p=subprocess.run([ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(video),'-af',
        'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],capture_output=True,text=True,encoding='utf-8',timeout=120)
    lo=json.loads(p.stderr[p.stderr.rfind('{'):p.stderr.rfind('}')+1])
    report=dict(file=video.name,duration=info.duration,width=info.width,height=info.height,fps=info.fps,
        has_audio=info.has_audio,full_decode=True,loudness=lo,words=total,matched=len(offsets),
        median_ms=round(float(np.median(offsets))*1000) if offsets else None,
        max_ms=round(max(abs(x) for x in offsets)*1000) if offsets else None,
        over_300ms=int(sum(abs(x)>.3 for x in offsets)),jitter_flips=int(qa.jitter_report(video,data['total'])))
    save(folder/'checks.json',report)
    ffmpeg.run(['-i',str(video),'-vf',f'fps=16/{data["total"]},scale=270:-2,tile=4x4',
        '-frames:v','1',str(folder/'finished_sheet.jpg')],timeout=120)
    print('CHECKED',key,json.dumps(report,ensure_ascii=False),flush=True)
    for row in rows:
        print(round(row['start'],2),round(row['end'],2),row['text'],flush=True)


def package():
    titles={
        '01_grace_polly':'Грейс рассказала секрет Томми. Ответ Полли | Острые козырьки',
        '02_brothers':'Для меня семья — сила. Томми и Артур | Острые козырьки',
        '03_our_city':'Кто встретит женщину с $10 000? | Острые козырьки',
    }
    lines=['# Три новых ролика из S03E01','',
        'Сохранены оформление одобренного пилота и исходная скорость речи. Ничего не опубликовано; '
        'очередь SQLite и расписание не менялись.','',
        '| Ролик | Длительность | Речь: совпало | Медиана сдвига | Пики звука |',
        '|---|---:|---:|---:|---:|']
    manifest=[]
    for key,plan in SCENES.items():
        folder=OUT/key
        latest=json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        c=json.loads((folder/'checks.json').read_text(encoding='utf-8'))
        assert c['full_decode'] and c['has_audio'] and c['file']==latest['video']
        assert c['matched']>=.75*c['words'],f'Слишком мало совпавших слов: {key}'
        assert float(c['loudness']['input_tp'])<0,f'Пик выше 0 dBTP: {key}'
        credit=json.loads((folder/'music_credit.json').read_text(encoding='utf-8'))
        description=(plan['title']+'.\nОстрые козырьки / Peaky Blinders, сезон 3, серия 1.\n\n'
            f"Музыка: {credit['title']} — {credit['creator']}.\nИсточник: {credit['foreign_landing_url']}\n"
            f"Лицензия: {credit['license_url']}\n\n#ОстрыеКозырьки #PeakyBlinders #Shorts")
        save(folder/'metadata_draft.json',dict(title=titles[key],description=description,
            tags=['Острые козырьки','Peaky Blinders','Томми Шелби','Shorts']))
        lines.append(f"| [{plan['title']}]({key}/{latest['video']}) | {c['duration']:.2f} с | "
            f"{c['matched']}/{c['words']} | {c['median_ms']:+d} мс | {c['loudness']['input_tp']} dBTP |")
        manifest.append(dict(key=key,**latest,checks=c))
    lines.extend(['','## Содержание','',
        '- **Грейс и Полли:** секреты Томми, хвастовство осведомлённостью, новая информация для Полли и ответ на выпад Грейс. '
        'Убраны технические подробности сделки и долгие паузы, ответ сохранён.',
        '- **Томми и Артур:** упрёк после речи, ссора, признание Артура и поддержка брата. '
        'Оставлен контекст, объясняющий причину конфликта, и финал о семье и доверии.',
        '- **Это наш город:** деньги не переданы на встрече, опасная перевозка наличных и ответ о том, кто её обеспечит. '
        'Диалог непрерывен.','',
        '## Оформление и музыка','',
        '1080×1200, 25 кадров/с; по 90 пикселей поля сверху/снизу, как в исправленном пилоте. '
        'Монтаж хронологический, без ускорения речи, случайной финальной нарезки и повторного cold open. '
        'В ночных сценах слегка поднята гамма. Для склеек между похожими тёмными крупными планами '
        'добавлено определение границ по резкому изменению пикселей: окно кадрирования переставляется на нового собеседника.',
        '',
        'Прежний шаблон After Effects: Montserrat SemiBold / ExtraBold Italic, две строки, цветные акценты, '
        'свечение и появление слов по речи. Сомнительно распознанное прилагательное в реплике Артура опущено '
        'из подписи; смысл «за кого ты меня принимаешь — за идиота?» сохранён. Ошибка «за мнем» исправлена на «замнём».',
        '',
        'Музыка различается: Mystery Of Notes — JeAnne, Rain — VolexOne, I Am a Robot — Alexi Action. '
        'Выбор сделан из локальных инструментальных треков по метаданным. Подложка приглушена под диалогом; '
        'подъём допускается только после последней реплики. Громкость финала нормализована после AE. '
        'Сведения об авторах и лицензиях включены в metadata_draft.json. Полного прослушивания здесь не было: '
        'соответствие музыки эмоции и разборчивость на телефоне требуют оценки при воспроизведении.',
        '',
        '## Проверки и границы результата','',
        'Каждый готовый MP4 полностью декодирован ffmpeg; проверены размеры, длительность, наличие аудио, '
        'границы субтитров, громкость и true peak. Готовый микс распознан заново Whisper large-v3. '
        'Для сравнения слов использованы те же нормализация и поиск совпадений, что в editbot.qa. '
        'Отчёты checks.json и sync_flags.json сохранены рядом с каждым роликом. Это эвристика: '
        'повторы, короткие слова и дефисы могут не сопоставляться. Несовпадение само по себе не доказывает ошибку субтитров.',
        '',
        'Дополнительно реплики братьев распознаны отдельно по каждому чистому монтажному фрагменту без музыки '
        '(clean_fragment_transcript.json). Эта проверка подтвердила, что большинство флагов длинного микса '
        'вызваны сдвигом распознавания: «хватит» 29.50 с при подписи 29.42 с, «вошли» 42.98 с '
        'при подписи 43.04 с, «ладно» 62.82 с при подписи 62.76 с. Исправлено начало первого '
        '«за кого». У Грейс первое слово перенесено на 0.54 с по повторному распознаванию.',
        '',
        'Просмотрены листы кадров исходных сцен, чистого монтажа и готовых файлов. Это не непрерывный просмотр со звуком '
        'и не гарантия удержания или просмотров. Видеофайлы готовы для оценки и выбора публикаций; автоматически не загружаются.',
        '',
        'Рядом сохранены AEP, монтажные MOV/WAV, план таймкодов, расшифровки и черновик метаданных. '
        'Для открытия AEP нужна папка media: её не удалять.',
        '',
        '## Команды','',
        '```powershell',
        '.\\.venv\\Scripts\\python.exe tools/series_batch.py prepare',
        '.\\.venv\\Scripts\\python.exe tools/refine_series_batch.py',
        '.\\.venv\\Scripts\\python.exe tools/series_batch.py render',
        '.\\.venv\\Scripts\\python.exe tools/series_batch.py check',
        '.\\.venv\\Scripts\\python.exe tools/series_batch.py package',
        '```','',
        'Рендер требует установленного After Effects и доступа к C:/aebridge. Новая ревизия не перезаписывает старый MP4. '
        'Для одной сцены добавьте --only и её ключ из manifest.json.'])
    (OUT/'README.md').write_text('\n'.join(lines),encoding='utf-8')
    save(OUT/'manifest.json',manifest)
    print('PACKAGED',len(manifest),'videos',flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,stream=sys.stdout,format='%(asctime)s %(message)s')
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['study','prepare','render','check','package'])
    p.add_argument('--only',choices=list(SCENES))
    args=p.parse_args()
    if args.action=='study': study()
    elif args.action=='package': package()
    else:
        model=whisper() if args.action=='check' else None
        for key in ([args.only] if args.only else SCENES):
            if args.action=='prepare': prepare(key)
            if args.action=='render': render(key)
            if args.action=='check': check(key,model)
