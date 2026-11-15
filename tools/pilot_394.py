"""Контрольный перемонтаж одной сцены; без изменения очереди и публикации."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from editbot import ffmpeg

OUT = ROOT/'review/pilot_394'
START, END = 662.90, 673.85


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source = ROOT/'library/Peaky Blinders/Season 2/03 Episode 3 (1080p HD).m4v'
    old = next((ROOT/'output/Острые_козырьки/s02e03').glob('3_*.mp4'))
    music = old.parent/'ae'/f'{old.stem}_media'/'music.wav'
    target = OUT/'bandages_rework_v2.mp4'
    if target.exists():
        raise SystemExit('Пробная версия уже существует; она не будет перезаписана')
    # Первая фраза и начала событий уточнены повторным распознаванием исходного
    # диалога. Термин «гангрены» исправлен отдельно; ASR путает это слово.
    events = [(.21,1.85,'Когда я сниму повязки?'),(2.17,3.85,'Когда я сам их сниму.'),
              (4.85,6.40,'Через неделю, не раньше.'),(6.93,8.15,'Гнилью не пахнет.'),
              (8.73,9.65,'Гангрены нет.')]
    ass = ['[Script Info]','ScriptType: v4.00+','PlayResX: 1080','PlayResY: 1080',
           '[V4+ Styles]',
           'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
           'Style: Default,Arial,58,&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,2.5,0,2,65,65,190,1',
           '[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    def stamp(t):
        n=round(t*100)
        return f'{n//360000}:{n//6000%60:02}:{n//100%60:02}.{n%100:02}'
    for a,b,text in events:
        ass.append(f'Dialogue: 0,{stamp(a)},{stamp(b)},Default,,0,0,0,,{text}')
    (OUT/'dialogue.ass').write_text('\n'.join(ass),encoding='utf-8')
    dur = END-START
    # Сохраняем всех участников: более широкий кадр вместо приближения, режущего лицо
    # врача. Два варианта звука при одинаковом видеоряде позволяют сравнить подложку.
    graph = ("[0:v]crop=iw*0.75:ih:iw*0.125:0,scale=1080:810,setsar=1,"
             "pad=1080:1080:0:135:black,subtitles=dialogue.ass[v];"
             "[0:a:0]loudnorm=I=-16:TP=-2:LRA=9,asplit=2[voice][key];"
             "[1:a]atrim=duration=10.95,asetpts=PTS-STARTPTS,"
             "loudnorm=I=-16:TP=-2:LRA=7,volume=0.16,"
             "afade=t=in:d=0.25,afade=t=out:st=10.25:d=0.7[bed];"
             "[bed][key]sidechaincompress=threshold=0.025:ratio=5:attack=15:release=200[duck];"
             "[voice][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:latency=1[a]")
    ffmpeg.run(['-ss',str(START),'-i',str(source),'-i',str(music),'-t',str(dur),
                '-filter_complex',graph,'-map','[v]','-map','[a]','-c:v','libx264',
                '-preset','slow','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k',
                '-movflags','+faststart',str(target)],cwd=OUT,timeout=600)
    # Контрольная версия с тем же видео и только исходным диалогом.
    clean = OUT/'bandages_dialogue_only_v2.mp4'
    ffmpeg.run(['-i',str(target),'-ss',str(START),'-i',str(source),'-t',str(dur),
                '-map','0:v:0','-map','1:a:0','-c:v','copy','-af','loudnorm=I=-16:TP=-2:LRA=9',
                '-c:a','aac','-b:a','192k','-movflags','+faststart',str(clean)],timeout=120)
    checks=[]
    for p in (target,clean):
        info=ffmpeg.probe(p)
        assert info.has_audio and (info.width,info.height)==(1080,1080)
        assert abs(info.duration-dur)<.15
        ffmpeg.run(['-i',str(p),'-f','null','-'],timeout=120)
        checks.append(dict(file=p.name,duration=info.duration,width=info.width,height=info.height,
                           has_audio=info.has_audio,full_decode=True))
    (OUT/'checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    (OUT/'edit_plan.json').write_text(json.dumps(dict(source=str(source),start=START,end=END,
        music_source=str(music),music_gain_db=-15.92,events=events,
        note='Музыка из старого AE-проекта, её соответствие настроению на слух не подтверждено.'),
        ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(checks,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
