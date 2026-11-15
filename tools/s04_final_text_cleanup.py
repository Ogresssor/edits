"""Apply visual-proofreading fixes without re-running ASR or changing movie cuts."""
import json
import s04_season_edit as e

def main():
    for key in ['P302','P401','P404']:
        folder=e.OUT/key;path=folder/'timeline.json';data=json.loads(path.read_text('utf-8'));changed=[]
        for event in data['subs']:
            for group in ['top','main']:
                expanded=[]
                for word in event[group]:
                    if key=='P302' and word['text']=='ШПЕТНИСТЫЙ':
                        changed.append('ШПЕТНИСТЫЙ → ПЯТНИСТЫЙ');word['text']='ПЯТНИСТЫЙ'
                    if key=='P404' and word['text']=='ЧЬЕЙ':
                        expanded.append(dict(t=max(0,word['t']-.08),text='В'));changed.append('Restore preposition в чьей тени')
                    if key=='P401' and word['text']=='ВЫЛЕЙ ЧАЙ':
                        expanded += [dict(t=word['t'],text='ВЫЛЕЙ'),dict(t=round(word['t']+.30,3),text='ЧАЙ')]
                        changed.append('Split вылей чай into two timed word reveals')
                    else:expanded.append(word)
                event[group]=expanded
        if changed:
            e.b.save(path,data);e.b.save(folder/'visual_text_corrections.json',changed)
            print('TEXT FIXED',key,flush=True)

if __name__=='__main__':main()
