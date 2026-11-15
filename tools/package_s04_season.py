"""Local review catalogue with explicit approval state and exact export hashes."""
import html
import json
import hashlib
from collections import Counter
import s04_season_edit as e

NOTES={
 'P101':'Короткая история: 27 гостей превращаются в 28. Приглашение и причина сохранены.',
 'P102':'Просьба о равной оплате → решение Томми → ответ Иден о стачке.',
 'P103':'Передача денег → подозрение → объяснение подготовки убийства. Предмет-улика сохранён.',
 'P201':'Отказ продать склад → ставка Томми → требование уважать собственность друзей. Жёсткий диалог.',
 'P202':'Разница в весе → демонстрация боя → объяснение отца → нокаут и реакция. Полная ширина для ударов.',
 'P203':'Лёгкий вход в кабинет → пистолет → объяснение разряженного оружия → вывод об охране.',
 'P204':'Спокойная комедия. В начале общий план лодки; визуальный вход слабее, чем у крупных диалогов.',
 'P302':'Жалоба на еду → напоминание о погибших → требования Луки → возвращение домой после дела.',
 'P303':'Линда хочет работать → возражение про грех → ответ про приём ставок.',
 'P401':'Просьба о гадании → чашка → вопрос о виски → раскрытие беременности. Пауза с ежедневником сокращена.',
 'P402':'Вопрос о сопернике → страх прожить жизнь зря → ответ тренера и имя настоящего соперника.',
 'P403':'Запрет женщинам → появление Ады → извинение бармена → финал со льдом.',
 'P404':'Представление страшного соперника → сомнение насчёт сына → спокойное согласие Гольда.',
 'P405':'Скворцы → пустельга → вопрос об итальянцах. Обе части сравнения сохранены.',
 'P501':'Признание про троих → вопрос ребёнка → ответ про кроватку. Использована фактическая формулировка LostFilm.',
 'P502':'Прайс за предательство → поручение Луки → согласие без торга → вывод Алфи. Длинный диалог, требует внимательного просмотра.',
 'P504':'Последние деньги на продукты → неожиданная покупка → объяснение желания другой жизни. Формулировки взяты из LostFilm, не из перевода SRT.',
 'P601':'Жалоба на плевки → новость Лиззи → язвительный вопрос → ответ Полли. Не ставить рядом с P401 при выборе публикаций.',
 'P602':'Требование подписать → Капоне → смена приказа охране → исход конфликта. Длинная история с дракой.',
 'P603':'Угроза Томми → просьба о собаке → признание о болезни → Алфи достаёт оружие → исход встречи и реакция Томми. Только эта сцена.',
}

def main():
    rows=[]
    for c in e.candidates():
        key=c['id'];folder=e.OUT/key
        latest=json.loads((folder/'latest.json').read_text('utf-8'))
        plan=json.loads((folder/'plan.json').read_text('utf-8'))
        integrity=json.loads((folder/'integrity.json').read_text('utf-8'))
        speech=json.loads((folder/'speech_check.json').read_text('utf-8'))
        assert latest['video']==integrity['file']==speech['file']
        assert latest['timeline_sha256']==hashlib.sha256((folder/'timeline.json').read_bytes()).hexdigest()
        assert latest.get('mix_version')==2 and speech['words']>0
        assert integrity['full_decode'] and not integrity['black_events'] and not integrity['freeze_flags']
        approval=json.loads((folder/'approval.json').read_text('utf-8'))
        row=dict(key=key,episode=int(key[1]),title=plan['title'],video=f"{key}/{latest['video']}",
            seconds=plan['duration'],sha256=integrity['sha256'],approval=approval.get('state','pending'),
            upload_authorized=bool(approval.get('upload_authorized',False)),
            editorial_note=NOTES[key],speech_check=speech,technical_check='passed')
        if row['upload_authorized']:
            assert approval['sha256']==row['sha256']
            row.update(upload_state=approval.get('upload_state'),youtube_id=approval.get('youtube_id'),
                       publish_at=approval.get('publish_at'),uploaded=bool(approval.get('youtube_id')))
        rows.append(row)
    e.b.save(e.OUT/'manifest.json',rows)
    long=[r['key'] for r in rows if r['seconds']>=60]
    lines=['# Острые козырьки — четвёртый сезон: монтажи для просмотра','',
        f"20 новых роликов из всех шести серий; вместе с одобренной музыкальной версией P301 — 21 история. "
        'Новые ролики ожидают просмотра, загрузка не разрешена.','',
        '[Открыть видеокаталог](index.html) · [Плейлист 20 новых роликов](ALL_EDITS.m3u8)','',
        '[Ранее одобренная Полли, P301](../s04_first/P301_polly_conditions/P301_polly_conditions_v2_music.mp4)','',
        '| Код | Серия | Ролик | Секунды | Комментарий |','|---|---:|---|---:|---|']
    cards=[]
    for r in rows:
        lines.append(f"| {r['key']} | {r['episode']} | [{r['title']}]({r['video']}) | {r['seconds']:.2f} | {r['editorial_note']} |")
        esc=html.escape
        cards.append(f'''<article data-episode="{r['episode']}"><h2>{r['key']} · {esc(r['title'])}</h2>
<p>Серия {r['episode']} · {r['seconds']:.2f} с · ожидает просмотра</p>
<video controls preload="none" src="{r['video']}"></video><p>{esc(r['editorial_note'])}</p>
<p><a href="{r['video']}">Открыть MP4</a></p></article>''')
    lines+=['','## Проверки и ограничения','',
        'Все новые экспорты прошли полное декодирование и сопоставление видеокадров с подготовленными исходниками. '
        'Проверены число кадров при 25 кадрах/с, отсутствие обнаруженных чёрных вставок и зависаний, код завершения AE, '
        'пики громкости и совпадение звуковых фрагментов на склейках. Повторное ASR отмечает спорные слова и тайминг в speech_check.json и sync_flags.json каждого ролика.','',
        'Просмотрены листы кадров, прочитаны диалоги и выполнено несколько проходов ASR, включая спорные короткие отрывки. '
        'Непрерывного просмотра и прослушивания всех монтажей человеком не было. Поэтому это партия для согласования, а не подтверждение публикационной готовности каждого файла.','',
        'В напряжённых сценах — одобренная подложка Dark Shapes; в спокойных и комедийных — более тихая East Side Bar (Instrumental). '
        'Подложка приглушается под речь. Новую музыку нужно оценить на слух. Источники и сведения о лицензии — в music_credit.json каждого ролика.','',
        'Длиннее минуты: '+', '.join(long)+'. Длительность сама по себе не гарантирует отсутствие претензий Content ID; проверка YouTube не выполнялась.','',
        'Рекомендуемый первый просмотр: P303, P403, P101, P203, P501, P401, P402, P404. '
        'P204 — более спокойный визуальный вход; P502 и P602 — длинные истории, их стоит оценить отдельно.','',
        'В резерве остаются 8 средних и 4 слабых кандидата. [Общая карта сцен и исходные просмотровые фрагменты](../new_sources_2026-11-11/index.html). В число этих 20 монтажей резерв не включён.']
    (e.OUT/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    playlist='#EXTM3U\n'+''.join(f"#EXTINF:{r['seconds']},{r['key']} {r['title']}\n{r['video']}\n" for r in rows)
    (e.OUT/'ALL_EDITS.m3u8').write_text(playlist,encoding='utf-8')
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Острые козырьки · монтажи S04</title><style>body{font:16px system-ui;background:#14171c;color:#eee;margin:24px auto;padding:0 20px;max-width:1200px}h1{font-size:28px}p{line-height:1.5;color:#cbd0d8}a{color:#79d5e5}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}article{padding:16px;background:#20252c;border-radius:12px}h2{font-size:19px}video{width:100%;max-height:450px;background:black}button{padding:9px 14px;margin:0 6px 12px 0;border:0;border-radius:6px;cursor:pointer}button[aria-pressed=true]{background:#79d5e5}article[hidden]{display:none}</style>
<h1>Острые козырьки · сезон 4</h1><p>20 новых монтажей для просмотра. Ничего из этой партии не загружено на YouTube.</p>
<p><a href="../s04_first/P301_polly_conditions/P301_polly_conditions_v2_music.mp4">Одобренная Полли, P301</a> · <a href="ALL_EDITS.m3u8">Плейлист</a> · <a href="README.md">Описание и проверки</a></p>
<nav><button data-filter="all" aria-pressed="true">Все серии</button>'''+''.join(f'<button data-filter="{i}" aria-pressed="false">Серия {i}</button>' for i in range(1,7))+'''</nav><main>'''+''.join(cards)+'''</main><script>
document.querySelectorAll('button').forEach(b=>b.onclick=()=>{document.querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',x===b));document.querySelectorAll('article').forEach(a=>{a.hidden=b.dataset.filter!=='all'&&a.dataset.episode!==b.dataset.filter;if(a.hidden)a.querySelector('video').pause()})});
document.querySelectorAll('video').forEach(v=>v.addEventListener('play',()=>document.querySelectorAll('video').forEach(x=>{if(x!==v)x.pause()})));
</script></html>'''
    (e.OUT/'index.html').write_text(page,encoding='utf-8')
    print('PACKAGED',len(rows),'new edits; episodes',dict(Counter(r['episode'] for r in rows)))

if __name__=='__main__':main()
