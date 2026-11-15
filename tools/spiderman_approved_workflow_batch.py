"""Six local edits from the whole-film shortlist. Publication is never performed."""
import argparse
import hashlib
import json
import logging
import time
import spiderman_first as film

OUT=film.ROOT/'review/spiderman_six_for_approval'
PLANS={
 '01_name':dict(candidate='S03',title='Питер придумал имя. Ведущий решил иначе',
    plate=['ПИТЕР УЖЕ ПРИДУМАЛ СЕБЕ ИМЯ.','НО У ВЕДУЩЕГО БЫЛ ДРУГОЙ ПЛАН.'],
    parts=[(2319.15,2347.90,1440)]),
 '02_photos':dict(candidate='S06',title='Он назвал снимки мусором. И сразу их купил',
    plate=['СНИМКИ ЕМУ НЕ ПОНРАВИЛИСЬ.','ПОЧЕМУ ЖЕ ОН ИХ ПОКУПАЕТ?'],
    parts=[(3682.95,3692.70,1440),(3697.85,3721.60,1440)]),
 '03_fired':dict(candidate='S07',title='Норман принёс компании успех. Совет уволил его',
    plate=['ЕГО КОМПАНИЯ БИЛА РЕКОРДЫ.','СОВЕТ ПРИГОТОВИЛ СЮРПРИЗ.'],
    parts=[(3783.13,3792.90,1440),(3803.04,3826.00,1440),(3829.20,3843.90,1440)]),
 '04_photographer':dict(candidate='S09',title='Джеймисон не выдал фотографа даже Гоблину',
    plate=['ГОБЛИН ТРЕБУЕТ ИМЯ ФОТОГРАФА.','ДЖЕЙМИСОН ЗНАЕТ ЕГО.'],
    parts=[(4507.95,4528.15,1440)]),
 '05_web':dict(candidate='S01',title='Паутина появилась. Инструкцию Питеру не дали',
    plate=['СПОСОБНОСТЬ УЖЕ ЕСТЬ.','КАК ЕЁ ВКЛЮЧИТЬ?'],
    parts=[(1610.30,1625.15,1440),(1626.80,1637.30,1920),(1646.40,1673.00,1920)]),
 '06_fire':dict(candidate='S10',title='Он вернулся в огонь за человеком. Кто его ждал?',
    plate=['В ОГНЕ ОСТАЛСЯ ЧЕЛОВЕК.','ИЛИ КТО-ТО ДРУГОЙ?'],
    parts=[(4999.45,5005.40,1440),(5018.90,5050.23,1920)]),
}


def select(key):
    p=PLANS[key]
    film.OUT=OUT;film.KEY=key;film.TITLE=p['title'];film.PLATE=p['plate'];film.PARTS=p['parts']
    film.select()


def polish_caps(key):
    path=OUT/key/'timeline.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    for e in data['subs']:
        for w in e['top']+e['main']:
            w['text']=w['text'].replace('КРОМИЛА','ГРОМИЛА')
            if key=='06_fire' and w['text']=='ОГОНЬ.':w['text']='НА ОГОНЬ.'
    film.batch.save(path,data)


def package():
    # Do not publish a review bundle based only on a decodable container.
    import check_six_render_integrity as integrity
    import check_six_speech_edges as speech
    integrity.main()
    speech.main()
    integrity_rows={r['key']:r for r in json.loads((OUT/'integrity_review/summary.json').read_text(encoding='utf-8'))}
    speech_rows={r['key']:r for r in json.loads((OUT/'integrity_review/speech_edges.json').read_text(encoding='utf-8'))}
    final=OUT/'FINAL';final.mkdir(exist_ok=True)
    rows=[]
    for i,(key,p) in enumerate(PLANS.items(),1):
        f=OUT/key
        latest=json.loads((f/'latest.json').read_text(encoding='utf-8'))
        c=json.loads((f/'checks.json').read_text(encoding='utf-8'))
        assert c['file']==latest['video'] and c['full_decode'] and c['has_audio'] and c['black_events']==[]
        assert float(c['loudness']['input_tp'])<0
        assert not c['words'] or c['matched']>=c['words']*.8
        source=f/latest['video'];dest=final/f'{key}.mp4'
        assert source.stat().st_mtime>=(f/'timeline.json').stat().st_mtime,'Video predates current edit'
        assert source.stat().st_mtime>=(f/latest['project']).stat().st_mtime,'Video predates current AE project'
        digest=hashlib.file_digest(source.open('rb'),'sha256').hexdigest()
        guard=integrity_rows[key]
        assert guard['sha256']==digest and guard['frame_count_ok'] and guard['p95_pixel_error']<8
        assert guard['mismatch_runs']==[] and guard['suspected_freezes']==[]
        assert all(not e['start_inside_word'] and not e['end_inside_word'] for e in speech_rows[key]['parts'])
        if dest.exists() and not dest.samefile(source):dest.unlink()
        if not dest.exists():dest.hardlink_to(source)
        rows.append(dict(number=i,key=key,candidate=p['candidate'],title=p['title'],
            review_file=dest.relative_to(OUT).as_posix(),approval='pending',uploaded=False,
            sha256=digest,**latest,checks=c))
    film.batch.save(OUT/'manifest.json',rows)
    lines=['# Шесть новых роликов: на согласование','',
        '**Ничего из этой партии на YouTube не загружено. Публикация только после явного согласования конкретных роликов.**','',
        'Отбор выполнен после карты всего фильма и повторной проверки десяти новых кандидатов. '
        'Эта шестёрка не использует три сцены, загруженные ранее. Сохранены порядок событий, целые реплики, '
        'русский дубляж, музыка и эффекты фильма. Дополнительной музыки нет. '
        'Привычные субтитры Montserrat и короткая плашка в начале; в действии оставлена ширина кадра.','',
        '| № | Ролик | Длительность |','|---|---|---:|']
    for r in rows:lines.append(f"| {r['number']} | [{r['title']}]({r['review_file']}) | {r['checks']['duration']:.1f} с |")
    lines.extend(['','[Посмотреть подряд](ALL_SIX.m3u8)','',
        'Проверены полное декодирование, размеры, частота кадров, наличие звука, пики, границы подписей '
        'и отсутствие чёрных вставок на склейках. Просмотрены листы кадров; речь повторно распознана из готовых файлов. '
        'Флаги ASR сохранены рядом с роликами; совпадение распознаваний не является гарантией точности каждого слова. '
        'Непрерывного прослушивания здесь не было. Техническая проверка не подтверждает отсутствие заявок Content ID.','',
        'Для правок достаточно номера ролика, времени и замечания. AEP, планы и медиа сохранены в папке каждого ролика.'])
    lines.extend(['', '[Проверка рендера и границ речи](integrity_review/README.md)'])
    (OUT/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    playlist=['#EXTM3U']
    for r in rows:playlist.extend([f"#EXTINF:{r['checks']['duration']},{r['number']}. {r['title']}",r['review_file']])
    (OUT/'ALL_SIX.m3u8').write_text('\n'.join(playlist)+'\n',encoding='utf-8')
    print('PACKAGED',len(rows),flush=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','render','check','package'])
    parser.add_argument('--only',choices=list(PLANS));parser.add_argument('--wait',action='store_true')
    args=parser.parse_args()
    OUT.mkdir(exist_ok=True)
    if args.action=='package':package()
    else:
        model=film.batch.whisper() if args.action in {'prepare','check'} else None
        for key in ([args.only] if args.only else PLANS):
            select(key)
            if args.action=='prepare':
                film.prepare(model)
                polish_caps(key)
                film.batch.save(OUT/key/'approval.json',dict(state='pending',upload_authorized=False))
            elif args.action=='render':film.batch.render(key)
            else:
                if args.wait:
                    latest=OUT/key/'latest.json';deadline=time.monotonic()+1500
                    while True:
                        if latest.exists():
                            meta=json.loads(latest.read_text(encoding='utf-8'))
                            video=OUT/key/meta['video']
                            if (video.exists() and video.stat().st_mtime>=(OUT/key/'timeline.json').stat().st_mtime
                                    and latest.stat().st_mtime>=(OUT/key/'timeline.json').stat().st_mtime):break
                        if time.monotonic()>deadline:raise TimeoutError(key)
                        time.sleep(2)
                    checked=OUT/key/'checks.json'
                    if checked.exists() and checked.stat().st_mtime>=video.stat().st_mtime:
                        prior=json.loads(checked.read_text(encoding='utf-8'))
                        if prior['file']==meta['video'] and prior.get('black_events')==[]:
                            print('ALREADY CHECKED',key,flush=True)
                            continue
                film.checks.check_final(key,model)
