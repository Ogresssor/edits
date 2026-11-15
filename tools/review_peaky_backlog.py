"""Recheck the seven remaining finals; never upload from this script."""
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import series_batch as batch

ROOT = batch.ROOT / 'review/s03_stories_batch2'
OUT = ROOT / 'review_2026-11-11'
EDITORIAL = {
    '02_e03_charlie_mother': (8, True, 'Понятный эмоциональный разговор: вопрос о маме, объяснение утраты, обещание сохранить память.'),
    '03_e03_arthur_disobeys': (5, False, 'Начало «С женой» лишено вопроса; две сцены требуют более ясной завязки.'),
    '04_e03_becoming_father': (7, True, 'Новость об отцовстве, реакция братьев и личное поздравление образуют законченную сцену.'),
    '05_e04_polly_no_regret': (8, True, 'Исповедь с ясным поворотом: сожаление не об убийстве, а об отсутствии раскаяния.'),
    '06_e04_women_strike': (6, False, 'Сюжет завершён, но большую часть 54 секунд занимает перечисление условий стачки; для первой очереди слабее остальных.'),
    '07_e05_johnny_heaven': (8, True, 'Комедийная завязка про рай, неприятное поручение, возвращение к метафоре в финале.'),
    '08_e06_alfie_limits': (8, True, 'Конфликт о похищенном сыне, ответное обвинение и признание Томми завершают спор.'),
}


def main():
    OUT.mkdir(exist_ok=True)
    results = []
    for row in batch.json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))[1:]:
        key = row['key']; folder = ROOT / key
        file = ROOT / row['review_file']
        timeline = json.loads((folder / 'timeline.json').read_text(encoding='utf-8'))
        plan = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
        info = batch.ffmpeg.probe(file)
        run = subprocess.run([batch.ffmpeg.ffmpeg_exe(), '-hide_banner', '-i', str(file),
            '-vf', 'crop=1080:810:0:24,blackdetect=d=0.08:pix_th=0.03:pic_th=0.98,freezedetect=n=-60dB:d=2',
            '-af', 'volumedetect', '-f', 'null', '-'], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180)
        (OUT / (key + '.log')).write_text(run.stderr, encoding='utf-8')
        black = re.findall(r'black_start:[^\r\n]+', run.stderr)
        freezes = re.findall(r'freeze_(?:start|end|duration):[^\r\n]+', run.stderr)
        words = [w for r in json.loads((folder / 'context_transcript.json').read_text(encoding='utf-8')) for w in r['words']]
        edges = []
        for clip, segment in zip(timeline['clips'], plan['segments']):
            a = segment[0]; b = a + clip['dur']
            edges.append(dict(start=a, end=b,
                start_inside=[w for w in words if w[0] < a < w[1]],
                end_inside=[w for w in words if w[0] < b < w[1]]))
        score, selected, reason = EDITORIAL[key]
        tech = run.returncode == 0 and info.has_audio and not black and abs(info.duration - timeline['total']) < .15
        result = dict(key=key, title=row['title'], sha256=hashlib.file_digest(file.open('rb'), 'sha256').hexdigest(),
            file=str(file.resolve()), checked_at=datetime.now(timezone.utc).isoformat(),
            duration=info.duration, full_decode=run.returncode == 0, black_events=black,
            freeze_flags=freezes, speech_edges=edges, technical_pass=tech,
            score=score, selected=selected and tech, reason=reason,
            limits='Source movie and MOVs absent; transcripts and contact sheets reviewed, no continuous full listening.')
        results.append(result)
        batch.save(OUT / (key + '.json'), result)
        print(key, 'TECH', tech, 'SCORE', score, 'SELECT', result['selected'], 'BLACK', black, 'FREEZE', freezes,
              'EDGES', [e for e in edges if e['start_inside'] or e['end_inside']], flush=True)
    batch.save(OUT / 'summary.json', results)


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
