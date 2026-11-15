"""Write local review links only after final checks pass for the current revision."""
import json
import s04_first_edit as edit


def main():
    edit.select();p=edit.FOLDER
    variants=json.loads((p/'variants.json').read_text(encoding='utf-8'))
    checks=json.loads((p/'checks.json').read_text(encoding='utf-8'))
    integrity=json.loads((p/'integrity.json').read_text(encoding='utf-8'))
    assert checks['file']==variants['original'] and checks['full_decode']
    assert [v['file'] for v in integrity['variants']]==[variants['original'],variants['music']]
    assert checks['over_300ms']==0
    edit.b.save(p/'review_manifest.json',dict(title='Полли вернётся, но на своих условиях',
        episode='Peaky Blinders, S04E03',source_start=451,source_end=494.6,
        video_seconds=43.6,variants=integrity['variants'],approval='pending',upload_authorized=False))
    a=variants['original'];b=variants['music'];folder=p.name
    text=f'''# Полли вернётся, но на своих условиях

Готов один новый сюжет, 43,6 секунды. «Острые козырьки», сезон 4, серия 3; исходный участок 07:31–08:14,6.

- [Вариант А — оригинальный звук]({folder}/{a})
- [Вариант Б — с тихой музыкой]({folder}/{b})

Это два варианта одной сцены для выбора; обе версии публиковать не следует. Согласование ожидается, разрешения на загрузку нет.

Ада просит Полли вести себя прилично. Полли требует объяснений, перечисляет цену своей работы на компанию и соглашается вернуться только на своих условиях. Финальная реплика завершена, после неё оставлена реакция.

Непрерывный диалог сохранён без внутренних звуковых склеек и ускорения. Кадрирование меняется на исходных сменах планов: общий план сохраняет всех участниц, крупный приближает говорящую. Субтитры — Montserrat, с выделением ключевых слов. Отдельно исправлено преждевременное появление слов в финальной реплике.

Вариант А сохраняет звук сериала. В варианте Б добавлена инструментальная подложка Dark Shapes — Master of the Subway Ninjas, с приглушением под речь. Данные лицензии и ссылка сохранены в music_credit.json; музыку ещё нужно оценить на слух при просмотре.

Проверка обоих MP4: полное декодирование, 1090 видеокадров при 25 кадрах/с, отсутствие обнаруженных чёрных кадров и зависаний, пики ниже 0 dBFS. Картинка музыкальной версии совпадает с основной. Звуковая дорожка сопоставлена с непрерывным подготовленным исходником. Повторное распознавание речи проверяет тайминг подписей; подмена имени «Ада» распознавателем отмечена в sync_flags.json.

Проверка выполнена инструментами анализа, повторным ASR и просмотром листов кадров. Это не непрерывный просмотр и прослушивание человеком и не проверка Content ID.

Подробные результаты: `{folder}/checks.json`, `{folder}/integrity.json`, `{folder}/review_manifest.json`.
'''
    (edit.OUT/'README.md').write_text(text,encoding='utf-8')
    (edit.OUT/'ALL_VARIANTS.m3u8').write_text('#EXTM3U\n'+f'{folder}/{a}\n{folder}/{b}\n',encoding='utf-8')
    print('Review links and manifest saved; publication not authorized.')


if __name__=='__main__':main()
