"""Local design previews: generated still, Windows TTS, synthetic music. Never publish."""
import base64
import html
import os
from pathlib import Path
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from edits import media
from edits.analysis import analyze
from edits.core import Store, read, save, canonical, sha256
from edits.render import render, check

PHRASES = ['Я не собираюсь платить.', 'Тогда сделка отменяется.', 'Вы уверены?']


def synthesize(folder):
    if all((folder/f'voice-{i}.wav').exists() for i in range(3)):
        return
    if os.name != 'nt':
        raise RuntimeError('This demonstration voice uses Windows Irina; supply voice-0.wav..voice-2.wav on other OS')
    output = str(folder).replace("'", "''")
    phrases = ','.join("'"+p.replace("'","''")+"'" for p in PHRASES)
    script = f"""$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$speech=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{
 $speech.SelectVoice('Microsoft Irina Desktop');$speech.Rate=-1
 $phrases=@({phrases})
 for($i=0;$i -lt 3;$i++) {{
  $speech.SetOutputToWaveFile((Join-Path '{output}' ('voice-'+$i+'.wav')))
  $speech.Speak($phrases[$i]);$speech.SetOutputToNull()
 }}
}} finally {{$speech.Dispose()}}
"""
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell.exe','-NoProfile','-WindowStyle','Hidden','-EncodedCommand',encoded],check=True)


def main():
    folder=ROOT/'.local/design-demo'; folder.mkdir(parents=True,exist_ok=True)
    synthesize(folder)
    image=ROOT/'docs/assets/demo-scene-v1.png'
    lengths=[]
    for i in range(3):
        with wave.open(str(folder/f'voice-{i}.wav')) as voice:
            lengths.append(voice.getnframes()/voice.getframerate())
    starts=[.65]
    for length in lengths[:-1]: starts.append(starts[-1]+length+.55)
    duration=round(starts[-1]+lengths[-1]+.8,2)
    source_key=canonical(dict(image=sha256(image),voices=[sha256(folder/f'voice-{i}.wav') for i in range(3)],
                              starts=starts,duration=duration,generator='zoom-2026-11-15.1'))
    source=folder/f'demonstration-source-{source_key[:12]}.mp4'
    if not source.exists():
        args=['-y','-loop','1','-i',str(image)]
        for i in range(3):args+=['-i',str(folder/f'voice-{i}.wav')]
        filters=['[0:v]scale=1080:1920,zoompan=z=1+on*0.00008:x=iw/2-iw/zoom/2:y=ih/2-ih/zoom/2:d=1:s=1080x1920:fps=25,format=yuv420p[v]']
        for i,start in enumerate(starts):
            filters.append(f'[{i+1}:a]aresample=48000,adelay={round(start*1000)}:all=1[a{i}]')
        filters.append('[a0][a1][a2]amix=inputs=3:duration=longest:normalize=0,apad=pad_dur=1[a]')
        args+=['-filter_complex',';'.join(filters),'-map','[v]','-map','[a]',
               '-c:v','libx264','-preset','fast','-crf','18','-c:a','aac','-b:a','192k','-t',str(duration),str(source)]
        media.run(args)
    music=folder/'generated-pad.wav'
    if not music.exists():
        media.run(['-y','-f','lavfi','-i',f'aevalsrc=0.08*(sin(2*PI*110*t)+0.5*sin(2*PI*164.81*t)+0.25*sin(2*PI*220*t)):s=48000:d={duration}',
                   '-af','afade=t=in:d=1,afade=t=out:st='+str(duration-1)+':d=1',str(music)])
    store=Store(folder/'workspace'); scene_map=analyze(source,store)
    plan=dict(id='design-demonstration',demonstration=True,source=str(source),scene_map=str(scene_map),
              rating='medium',audio_track=0,story={k:'Demonstration only: generated image and synthetic dialogue, not a film scene' for k in
              ('hook','development','payoff','context','selection_reason')},
              on_screen_hook='Он отказался платить',segments=[dict(start=0,end=duration)],
              captions=[dict(start=round(s,3),end=round(s+n,3),text=text,highlight=accent)
                        for s,n,text,accent in zip(starts,lengths,PHRASES,['платить.','отменяется.','уверены?'])],
              music=dict(path=str(music),creator='Local sine-wave test generator',title='Synthetic ambient pad',
                         source_url='local:test-generator',license_url='local:demonstration-only-not-for-publication'))
    path=folder/'plan.json';save(path,plan)
    variants=[]
    labels={'clean':'01 — Чистое кино','accent':'02 — Сильная реплика','intrigue':'03 — Интрига'}
    for profile in labels:
        file=render(path,store,profile)
        report=check(file,store)
        if not report['passed']:raise RuntimeError(str(report))
        assert report['demonstration'] and not store.queue()
        receipt=read(file.parent/'receipt.json')
        poster_at = starts[0]+.8 if profile == 'intrigue' else starts[1]+.8
        media.run(['-y','-ss',str(round(poster_at,3)),'-i',str(file),'-frames:v','1','-vf','scale=360:640',str(folder/f'{profile}.jpg')])
        variants.append(dict(profile=profile,label=labels[profile],video=file.relative_to(folder).as_posix(),
                             qa=report,seconds=duration,render_seconds=receipt['elapsed_seconds']))
        print('DEMO_CHECKED',profile,report['sha256'],flush=True)
    save(folder/'manifest.json',dict(demonstration_only=True,variants=variants,voice='Microsoft Irina Desktop',
                                   source='AI-generated still with subtle digital zoom, not an acted film scene'))
    cards=''.join(f'<article><h2>{v["label"]}</h2><video controls playsinline preload="metadata" poster="{v["profile"]}.jpg" src="{html.escape(v["video"],quote=True)}"></video></article>' for v in variants)
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Варианты оформления</title><style>
    *{box-sizing:border-box}body{margin:0;background:#101114;color:#f6f5f1;font:16px system-ui,sans-serif}main{max-width:1160px;margin:auto;padding:32px 22px}h1{font-size:28px;margin:0 0 10px}p{color:#c8c8cb;max-width:800px;line-height:1.5}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin-top:28px}h2{font-size:16px}video{width:100%;aspect-ratio:9/16;background:#080808;border-radius:12px}button{padding:10px 18px;background:#d6ef85;color:#101114;border:0;border-radius:8px;font:inherit;cursor:pointer}@media(max-width:700px){.grid{grid-template-columns:1fr}article{max-width:360px;margin:auto;width:100%}}
    </style><main><h1>Одна сцена — три оформления</h1><p>Демонстрация: сгенерированное изображение с медленным наездом, синтетическая речь и музыкальная подложка. Это не монтаж фильма и не материал для публикации.</p><button id="pause">Остановить все</button><div class="grid">'''+cards+'''</div></main><script>const videos=[...document.querySelectorAll('video')];document.getElementById('pause').onclick=()=>videos.forEach(v=>v.pause());videos.forEach(v=>v.addEventListener('play',()=>videos.forEach(other=>{if(other!==v)other.pause()})));</script></html>'''
    (folder/'index.html').write_text(page,encoding='utf-8')
    print('GALLERY',folder/'index.html',flush=True)


if __name__=='__main__':main()
