"""Editorial batch for S03E02–06, isolated from publication queue."""
import argparse
import json
import logging
import sys
import time
import math
import re
import subprocess
from pathlib import Path
import series_batch as batch

ROOT = batch.ROOT
OUT = ROOT/'review/s03_remaining'
MUSIC = 'jamendo_b6bccae4'
PLANS = {
    'e02_john_negotiation': dict(episode=2, title='Переговоры закончились угрозой Джона',
        context=[815, 902], segments=[(829.30, 856.10), (857.10, 863.20),
            (865.90, 882.60), (889.10, 901.40)], gamma=1.05, crop_x_by_part={'1':780}),
    'e03_choose_fear': dict(episode=3, title='Выбирайте, кого боитесь больше',
        context=[2740, 2826], segments=[(2779.60, 2822.50)], gamma=1.18),
    'e03_father_fortune': dict(episode=3, title='Отец Томми предсказывал кражи лошадей',
        context=[3125, 3163], segments=[(3139.98, 3156.00)], gamma=1.02),
    'e04_linda_accounts': dict(episode=4, title='Линда пересчитала долю Артура',
        context=[2111, 2215], segments=[(2120.70, 2131.10), (2134.30, 2162.20),
            (2182.30, 2189.80), (2190.50, 2205.70), (2207.80, 2210.60)], gamma=1.03),
    'e05_old_testament': dict(episode=5, title='Артур объяснил Алфи свою веру',
        context=[921, 1029], segments=[(926.30, 944.70), (990.90, 1025.50)], gamma=1.04,
        crop_x_by_part={'1':410}, crop_x_ranges={'0':[[0,12.04,0],[16.04,18.4,0]]}),
    'e05_alfie_glasses': dict(episode=5, title='Очки Алфи показывают будущее',
        context=[638, 722], segments=[(648.00, 661.30), (664.40, 665.90),
            (668.20, 681.20), (682.40, 706.90), (715.90, 719.70)], gamma=1.04,
        crop_x_ranges={'2':[[2.68,9.36,820]], '3':[[5.72,9.64,820]], '4':[[.28,3.8,820]]}),
    'e06_alfie_trap': dict(episode=6, title='Томми поймал Алфи на пропущенном имени',
        context=[1232, 1308], segments=[(1243.40, 1304.45)], gamma=1.18),
}
for plan in PLANS.values():
    plan.update(music=MUSIC, bed=-24)


def select(key):
    batch.OUT = OUT
    batch.SOURCE = next((ROOT/'library/Peaky.Blinders.S03.1080p.BluRay').glob(
        f'*S03E{PLANS[key]["episode"]:02}*.mkv'))
    batch.SCENES = {key: PLANS[key]}


def correct_names(key):
    path = OUT/key/'context_transcript.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    backup = path.with_name('context_transcript_before_corrections.json')
    if not backup.exists():
        batch.save(backup, data)
    for row in data:
        row['text'] = row['text'].replace('Шауби', 'Шелби').replace('Ринда', 'Линда')
        for w in row['words']:
            w[2] = w[2].replace('Шауби', 'Шелби').replace('Ринда', 'Линда')
        if key=='e04_linda_accounts':
            # Arithmetic pauses were misheard as the preposition «в».
            row['words'] = [w for w in row['words'] if not (
                w[2]=='в' and (abs(w[0]-2143.24)<.02 or abs(w[0]-2159.66)<.02))]
            for w in row['words']:
                if abs(w[0]-2160.56)<.02 and w[2]=='тысячу':
                    w[2]='тысяча'
    batch.save(path, data)


def refine(key, model):
    import cv2
    folder=OUT/key
    path=folder/'timeline.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    backup=folder/'timeline_before_final_refinement.json'
    if not backup.exists():
        batch.save(backup,data)
    heard=[]
    rows=[]
    for clip in data['clips']:
        cap=cv2.VideoCapture(clip['file'])
        frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        assert frames>0
        clip['last_frame']=round((frames-1)/25, 4)
        segs,_=model.transcribe(clip['file'], language='ru', word_timestamps=True,
            vad_filter=False, beam_size=5, condition_on_previous_text=False)
        for seg in segs:
            ws=[[clip['start']+w.start,clip['start']+w.end,w.word.strip()] for w in seg.words or []]
            rows.append(dict(start=clip['start']+seg.start,text=seg.text,words=ws))
            heard.extend((w[0],batch.qa._norm(w[2])) for w in ws)
    batch.save(folder/'clean_fragment_transcript.json', rows)
    mixed=json.loads((folder/'rendered_transcript.json').read_text(encoding='utf-8'))
    mix=[(w[0],batch.qa._norm(w[2])) for row in mixed for w in row['words']]
    changes=[]
    previous=0.0
    for ev in data['subs']:
        words=ev['top']+ev['main']
        old=[ev['start']+w['t'] for w in words]
        new=list(old)
        for i,(t,w) in enumerate(zip(old,words)):
            n=batch.qa._norm(w['text'])
            ht=batch.qa._heard_at(heard,t,n) if n else None
            mt=batch.qa._heard_at(mix,t,n) if n else None
            # Only move words when two independent recognitions agree, away from clip starts.
            if ht is not None and mt is not None and ht>.18 and abs(ht-mt)<.35 and .30<abs(ht-t)<=2.2:
                new[i]=ht
        for i in range(1,len(new)):
            new[i]=max(new[i],new[i-1]+.04)
        new[0]=max(new[0],previous-.15)
        if new!=old and new[-1]<ev['end']-.12:
            for w,t,nt in zip(words,old,new):
                if abs(t-nt)>.01:
                    changes.append(dict(text=w['text'],before=round(t,3),after=round(nt,3)))
            ev['start']=round(new[0],3)
            for w,nt in zip(words,new):
                w['t']=round(nt-ev['start'],3)
        previous=ev['start']+words[-1]['t']
    data['total']=round(math.floor(data['total']*25+.5)/25,2)
    data['clips'][-1]['dur']=round(data['total']-data['clips'][-1]['start'],3)
    for ev in data['subs']:
        ev['end']=min(ev['end'],data['total'])
    batch.save(path,data)
    batch.save(folder/'caption_refinements.json',changes)
    print('REFINED',key,changes,flush=True)


def finalize_music(key):
    """Use the accepted quiet/ducked mix; AE contains captions and original speech only."""
    select(key)
    folder = OUT/key
    latest = json.loads((folder/'latest.json').read_text(encoding='utf-8'))
    original = folder/latest['video']
    target = folder/(original.stem+'_quiet.mp4')
    if target.exists():
        raise FileExistsError(target)
    duration = batch.ffmpeg.probe(original).duration
    graph = (
        '[1:a]aresample=48000,apad,asplit=2[voice][key];'
        f'[2:a]aresample=48000,apad,atrim=duration={duration},'
        'loudnorm=I=-16:TP=-3:LRA=9,aresample=48000,volume=-24dB,'
        f'afade=t=in:d=1,afade=t=out:st={duration-1}:d=1[bed];'
        '[bed][key]sidechaincompress=threshold=0.035:ratio=5:attack=15:release=350[ducked];'
        '[voice][ducked]amix=inputs=2:duration=first:normalize=0,'
        'loudnorm=I=-16:TP=-3:LRA=9,aresample=48000,'
        'alimiter=limit=0.7:level=false:latency=true,volume=-3dB[a]')
    batch.ffmpeg.run(['-i', str(original), '-i', str(folder/'clean_cut.mp4'),
        '-i', str(folder/'media/music.wav'), '-filter_complex', graph,
        '-map', '0:v:0', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
        '-t', str(duration), '-movflags', '+faststart', str(target)], timeout=180)
    latest.update(video=target.name, before_audio_mix=original.name,
        audio_recipe='Rain, -24 dB, sidechain, final -3 dB; approved Grace variant')
    batch.save(folder/'latest.json', latest)
    print('QUIET MIX', key, flush=True)


def check_final(key, model):
    batch.check(key, model)
    folder=OUT/key
    c=json.loads((folder/'checks.json').read_text(encoding='utf-8'))
    p=subprocess.run([batch.ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(folder/c['file']),
        '-vf','blackdetect=d=0.01:pix_th=0.03:pic_th=0.99','-an','-f','null','-'],
        capture_output=True,text=True,encoding='utf-8',check=True)
    events=[dict(start=float(a),end=float(b),duration=float(d)) for a,b,d in re.findall(
        r'black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)',p.stderr)]
    c['black_events']=events
    batch.save(folder/'checks.json',c)
    assert not events, f'Unexpected black frame: {key}: {events}'
    print('NO BLACK FRAMES',key,flush=True)


def package():
    manifest = []
    final_dir=OUT/'FINAL'
    final_dir.mkdir(exist_ok=True)
    lines = ['# Новая партия: серии 2–6', '',
        'Семь самостоятельных сцен. Монтаж и субтитры — как в одобренном пилоте. '
        'Музыка — тихая Rain с приглушением под речь, по выбранному варианту Грейс и Полли. '
        'Публикации и расписание не менялись.', '',
        '| Серия | Ролик | Секунды | Проверка речи |', '|---|---|---:|---:|']
    for index, (key, plan) in enumerate(PLANS.items(),1):
        folder = OUT/key
        latest = json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        c = json.loads((folder/'checks.json').read_text(encoding='utf-8'))
        assert c['file']==latest['video'] and c['full_decode'] and c['has_audio']
        assert c.get('black_events')==[]
        assert float(c['loudness']['input_tp'])<0
        assert c['matched']>=c['words']*.75
        credit = json.loads((folder/'music_credit.json').read_text(encoding='utf-8'))
        batch.save(folder/'metadata_draft.json', dict(title=plan['title']+' | Острые козырьки',
            description=f"Острые козырьки / Peaky Blinders. Сезон 3, серия {plan['episode']}.\n"
            f"Музыка: {credit['title']} — {credit['creator']}.\n"
            f"{credit['foreign_landing_url']}\n{credit['license_url']}\n"
            '#ОстрыеКозырьки #PeakyBlinders #Shorts'))
        review_file=f'FINAL/{index:02}_{key}.mp4'
        dest=OUT/review_file
        source=folder/latest['video']
        if dest.exists() and not dest.samefile(source):
            dest.unlink()
        if not dest.exists():
            dest.hardlink_to(source)
        lines.append(f"| {plan['episode']} | [{plan['title']}]({review_file}) | "
            f"{c['duration']:.1f} | {c['matched']}/{c['words']} |")
        manifest.append(dict(key=key, title=plan['title'], episode=plan['episode'],
            review_file=review_file, **latest, checks=c))
    lines.extend(['', '## Проверено', '',
        'Каждый MP4 полностью декодирован. Проверены разрешение 1080×1200, частота 25 fps, '
        'длительность, наличие звука, границы подписей, true peak и отсутствие чёрных вспышек. '
        'На границе слоя удерживается последний кадр, если MOV короче на один кадр. '
        'В FINAL находятся только семь последних версий, без промежуточных рендеров. Сохранены повторные '
        'расшифровки готовых файлов и флаги синхронизации. Сравнение ASR эвристическое, '
        'особенно при повторах и именах. Просмотрены листы кадров. '
        'Непрерывного просмотра со звуком здесь не было; эмоциональное соответствие музыки '
        'нужно оценить при просмотре партии.', '',
        'Для правок укажи ключ ролика или название, время и что изменить. '
        'AEP и media сохранены рядом; финальный тихий микс выполнен после AE скриптом '
        'tools/remaining_season.py, поэтому AEP содержит речь, а готовый MP4 — финальную музыку.', '',
        'Исходные встроенные SRT использованы только для поиска сцен; подписи получены '
        'из русской озвучки AlexFilm. Все склейки хронологические, скорость речи не менялась.'])
    (OUT/'README.md').write_text('\n'.join(lines), encoding='utf-8')
    batch.save(OUT/'manifest.json', manifest)
    playlist=['#EXTM3U']
    for item in manifest:
        playlist.extend([f"#EXTINF:{item['checks']['duration']:.2f},{item['title']}",
                         item['review_file']])
    (OUT/'ALL_7.m3u8').write_text('\n'.join(playlist)+'\n', encoding='utf-8')
    print('PACKAGED', len(manifest), flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['study', 'prepare', 'refine', 'render', 'check', 'package'])
    p.add_argument('--only', choices=list(PLANS))
    p.add_argument('--wait-for-render', action='store_true', help='Check each final mix as the sequential AE batch finishes')
    args = p.parse_args()
    if args.action=='package':
        package()
    else:
        model = batch.whisper() if args.action in {'check','refine'} else None
        for key in ([args.only] if args.only else PLANS):
            select(key)
            if args.action=='check' and args.wait_for_render:
                deadline=time.monotonic()+1200
                while True:
                    latest_path=OUT/key/'latest.json'
                    if latest_path.exists():
                        latest=json.loads(latest_path.read_text(encoding='utf-8'))
                        if (latest['video'].endswith('_quiet.mp4') and (OUT/key/latest['video']).exists()
                                and latest_path.stat().st_mtime>=(OUT/key/'timeline.json').stat().st_mtime):
                            break
                    if time.monotonic()>deadline:
                        raise TimeoutError(f'Render did not finish: {key}')
                    time.sleep(2)
                checks=OUT/key/'checks.json'
                if checks.exists() and checks.stat().st_mtime>=(OUT/key/'timeline.json').stat().st_mtime:
                    c=json.loads(checks.read_text(encoding='utf-8'))
                    if c['file']==latest['video'] and c.get('black_events')==[]:
                        print('ALREADY CHECKED', key, flush=True)
                        continue
            if args.action=='study':
                if not (OUT/key/'context_transcript.json').exists():
                    batch.study()
            elif args.action=='prepare':
                correct_names(key)
                batch.prepare(key)
                path = OUT/key/'timeline.json'
                data = json.loads(path.read_text(encoding='utf-8'))
                # Avoid any accidental double bed before the final sidechain mix.
                data['music']['levels'] = [[0, -96], [data['total'], -96]]
                batch.save(path, data)
            elif args.action=='render':
                batch.render(key)
                finalize_music(key)
            elif args.action=='refine':
                refine(key,model)
            elif args.action=='check':
                check_final(key, model)
