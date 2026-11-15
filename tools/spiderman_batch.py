"""Two distinct stories following the accepted Spider-Man style."""
import argparse
import logging
import spiderman_first as film

PLANS={
    'mj_tray':dict(title='Питер поймал всё. Кроме слов для Мэри Джейн',
        plate=['РЕФЛЕКСЫ УЖЕ ПОЯВИЛИСЬ.','УВЕРЕННОСТЬ — ЕЩЁ НЕТ.'],
        parts=[(1322.00,1346.00,1440),(1349.60,1353.10,1440)]),
    'not_my_problem':dict(title='Эти слова вернулись к нему через минуту',
        plate=['ОН НЕДОПЛАТИЛ ПИТЕРУ.','И ВСКОРЕ ОБ ЭТОМ ПОЖАЛЕЛ.'],
        parts=[(2512.50,2531.80,1440),(2539.00,2545.90,1920),
               (2552.00,2561.20,1920),(2562.00,2575.20,1440)]),
}


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','render','check'])
    parser.add_argument('--only',choices=list(PLANS))
    args=parser.parse_args()
    model=film.batch.whisper() if args.action in {'prepare','check'} else None
    for key in ([args.only] if args.only else PLANS):
        p=PLANS[key];film.KEY=key;film.TITLE=p['title'];film.PLATE=p['plate'];film.PARTS=p['parts'];film.select()
        if args.action=='prepare':
            film.prepare(model)
            film.batch.save(film.OUT/key/'metadata_draft.json',dict(
                title=p['title']+' | Человек-паук',
                description=p['title']+'.\nЧеловек-паук / Spider-Man (2002).\nРусский дубляж. Музыка и звуковые эффекты — из фильма, дополнительная музыка не добавлена.\n#ЧеловекПаук #SpiderMan #ТобиМагуайр #Shorts',
                tags=['Человек-паук','Spider-Man','Тоби Магуайр','Питер Паркер','Shorts']))
        elif args.action=='render':film.batch.render(key)
        else:film.checks.check_final(key,model)
