"""Targeted independent verification of flagged captions on clean scene fragments."""
import json
from series_batch import OUT, SCENES, whisper, save, qa


def main():
    model = whisper()
    key = '02_brothers'
    folder = OUT / key
    heard, rows, offset = [], [], 0.0
    for i, (a, b) in enumerate(SCENES[key]['segments']):
        segs, _ = model.transcribe(str(folder / 'media' / f'part_{i:02}.mov'),
            language='ru', word_timestamps=True, vad_filter=False, beam_size=5,
            condition_on_previous_text=False)
        for s in segs:
            words = [[round(offset+w.start, 3), round(offset+w.end, 3), w.word.strip()]
                     for w in s.words or []]
            rows.append(dict(start=offset+s.start, end=offset+s.end, text=s.text, words=words))
            heard.extend((w[0], qa._norm(w[2])) for w in words)
        offset = round(offset+b-a, 2)
    save(folder/'clean_fragment_transcript.json', rows)
    data = json.loads((folder/'timeline.json').read_text(encoding='utf-8'))
    # Only previously flagged words with unambiguous local matches are candidates.
    candidates = {'кого', 'хватит', 'замнем', 'вошли', 'ладно'}
    chosen = []
    for ev in data['subs']:
        for w in ev['top']+ev['main']:
            n = qa._norm(w['text'])
            if n in candidates:
                t = ev['start']+w['t']
                h = qa._heard_at(heard, t, n)
                print(round(t, 3), w['text'], h, flush=True)
                if h is not None:
                    chosen.append((h, n))
    save(folder/'timeline_before_refinement.json', data)
    print('BROTHERS FIXED', qa.realign(data['subs'], chosen), flush=True)
    save(folder/'timeline.json', data)
    folder = OUT/'01_grace_polly'
    data = json.loads((folder/'timeline.json').read_text(encoding='utf-8'))
    save(folder/'timeline_before_refinement.json', data)
    print('GRACE FIXED', qa.realign(data['subs'], [(.54, qa._norm('Знаешь'))]), flush=True)
    save(folder/'timeline.json', data)


if __name__ == '__main__':
    main()
