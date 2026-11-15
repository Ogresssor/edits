"""Local S04 editorial batch. No YouTube operations or upload authorization."""
import argparse
import json
import logging
import re
import sys
import subprocess
import math
import time
import uuid
import hashlib
import shutil
import numpy as np
from pathlib import Path
import s04_first_edit as first
import series_batch as b

ROOT=b.ROOT
OUT=ROOT/'review/s04_season'
CATALOG=ROOT/'review/new_sources_2026-11-11/candidates.json'
PLANS={
 'P101':dict(segments=[(1462.04,1466.60),(1472.52,1479.60),(1511.96,1525.40)],plate=['ЗА СТОЛОМ БУДЕТ 27 ГОСТЕЙ','НО ОДИН ЕЩЁ НЕ УЧТЁН'],mood='warm'),
 'P102':dict(segments=[(1360.88,1395.24)],plate=['ОНА ПОПРОСИЛА','РАВНУЮ ЗАРПЛАТУ'],mood='tense'),
 'P103':dict(segments=[(2847.84,2856.04),(2948.24,2969.72),(3015.48,3027.04),(3042.48,3057.52),(3061.92,3064.00)],plate=['ДЕСЯТЬ ФУНТОВ','ВЫДАЛИ ЧУЖОГО В ДОМЕ'],mood='tense'),
 'P201':dict(segments=[(1608.48,1613.92),(1619.64,1623.36),(1627.40,1629.96),(1646.04,1666.32),(1678.00,1687.72),(1759.64,1781.52)],plate=['ОН РЕШИЛ ЗАБРАТЬ','СКЛАД ДРУГА ТОММИ'],mood='tense'),
 'P202':dict(segments=[(2444.00,2455.00),(2462.24,2468.64),(2505.00,2513.00),(2530.40,2563.40)],plate=['ТЯЖЕЛОВЕС ПРОТИВ ПАРНЯ','КОТОРЫЙ НЕ ДАЁТ СДАЧИ'],mood='action',wide=True),
 'P203':dict(segments=[(3150.24,3188.08),(3243.52,3260.56)],plate=['ТОММИ ПОТЯНУЛСЯ','ЗА ПИСТОЛЕТОМ'],mood='tense',wide=True),
 'P204':dict(segments=[(2347.28,2369.04)],plate=['ИХ ПРИШЛИ УБИТЬ','НО КЁРЛИ СПОКОЕН'],mood='comic'),
 'P302':dict(segments=[(1352.32,1364.12),(1369.84,1378.68),(1384.00,1389.04),(1403.76,1425.00)],plate=['ОН ПОЖАЛОВАЛСЯ НА ЕДУ','ЛУКА НАПОМНИЛ О ДЕЛЕ'],mood='tense',height=880),
 'P303':dict(segments=[(2794.68,2827.28)],plate=['ЛИНДА ПОШЛА','ПРИНИМАТЬ СТАВКИ'],mood='comic'),
 'P401':dict(segments=[(1732.48,1746.16),(1750.60,1753.00),(1765.20,1776.32),(1805.64,1807.76),(1810.76,1840.80)],plate=['ЛИЗЗИ ПОПРОСИЛА','ПОГАДАТЬ НА ЧАЕ'],mood='warm'),
 'P402':dict(segments=[(1939.04,1976.64)],plate=['КОГО ОН УВИДИТ','В ДРУГОМ УГЛУ РИНГА?'],mood='reflective',height=880),
 'P403':dict(segments=[(2669.36,2677.40),(2679.00,2684.20),(2701.48,2730.16)],plate=['ЖЕНЩИН БЕЗ МУЖЧИН','В ЭТОМ ПАБЕ НЕ ОБСЛУЖИВАЮТ'],mood='comic'),
 'P404':dict(segments=[(3195.88,3216.76),(3229.92,3234.72),(3238.92,3249.80),(3257.84,3261.00)],plate=['АЛФИ ПРЕДСТАВИЛ СОПЕРНИКА','ГОЛЬД ПРИВЁЛ СЫНА'],mood='comic'),
 'P405':dict(segments=[(3019.76,3045.00)],plate=['ПУСТЕЛЬГА ПРОТИВ СКВОРЦОВ','А ПРОТИВ ИТАЛЬЯНЦЕВ?'],mood='tense'),
 'P501':dict(segments=[(785.76,803.88),(818.28,827.60)],plate=['ТОММИ ПОЛОЖИЛ ТРОИХ','СЫН УСЛЫШАЛ ЭТО'],mood='comic',title='Томми положил троих. Сын понял по-своему'),
 'P502':dict(segments=[(1959.00,1989.28),(2005.00,2022.80),(2031.16,2042.20),(2116.16,2146.48)],plate=['АЛФИ ВЫСТАВИЛ СЧЁТ','ЗА ПРЕДАТЕЛЬСТВО ДРУГА'],mood='tense',height=880),
 'P504':dict(segments=[(3065.52,3108.00)],plate=['ЕГО ПОСЛАЛИ ЗА ПРОДУКТАМИ','ОН КУПИЛ СОВСЕМ ДРУГОЕ'],mood='reflective',title='Почему Томми потратил последние деньги не на хлеб'),
 'P601':dict(segments=[(538.44,546.60),(559.00,587.20),(591.00,596.44)],plate=['ЛИНДА ПОПЫТАЛАСЬ УКОЛОТЬ','ЛИЗЗИ ПРИ ПОЛЛИ'],mood='comic'),
 'P602':dict(segments=[(1992.32,2010.40),(2037.60,2071.88),(2073.36,2087.64),(2110.00,2131.00)],plate=['ЛУКА ПРИШЁЛ ЗА ПОДПИСЬЮ','ЕГО ЛЮДИ УЖЕ ВЫБРАЛИ'],mood='action',wide=True),
 'P603':dict(segments=[(2394.52,2402.48),(2420.32,2441.20),(2460.84,2476.20),(2491.80,2494.40)],plate=['АЛФИ ЗНАЛ, ЗАЧЕМ ПРИШЁЛ ТОММИ','НО ПОПРОСИЛ ЗА СОБАКУ'],mood='reflective',wide=True,title='Алфи попросил позаботиться о собаке. Потом достал оружие'),
}


def framing(source,a,z,wide=False,height=1040):
    """Static framing inside original shots; include all detected faces or keep full width."""
    import cv2
    from editbot import reframe
    raw=subprocess.run([b.ffmpeg.ffmpeg_exe(),'-v','error','-ss',str(a),'-i',source,'-t',str(z-a),
        '-vf','scale=480:260','-an','-f','rawvideo','-pix_fmt','rgb24','-'],capture_output=True,check=True).stdout
    frames=np.frombuffer(raw,np.uint8).reshape(-1,260,480,3)
    hist=[];cuts=[0];last=-100
    for i,f in enumerate(frames):
        gray=cv2.cvtColor(f,cv2.COLOR_RGB2GRAY)
        h=cv2.calcHist([gray],[0],None,[32],[0,256]).ravel()/gray.size
        if hist and np.abs(h-hist[-1]).sum()>.45 and i-last>=9:cuts.append(i);last=i
        hist.append(h)
    cuts.append(len(frames));detector=reframe._detector(480,260);shots=[]
    for lo,hi in zip(cuts,cuts[1:]):
        bounds=[]
        if not wide:
            for idx in range(lo,hi,6):
                _,faces=detector.detect(cv2.cvtColor(frames[idx],cv2.COLOR_RGB2BGR))
                if faces is None:continue
                largest=max(f[2]*f[3] for f in faces)
                for f in faces:
                    if f[2]*f[3]>=largest*.18:bounds.append((float(f[0]),float(f[0]+f[2])))
        width=1920;x=0
        if bounds:
            left=min(x[0] for x in bounds)-18;right=max(x[1] for x in bounds)+18
            if right-left<=360:
                center=(left+right)/2
                width=1440;x=int(round(max(0,min(120,center-180))*4/2)*2)
        shots.append(dict(first=lo,last=hi,width=width,x=x,height=height))
    # The native frame count, not floating point duration, controls the boundaries.
    assert len(frames)==round((z-a)*25),(a,z,len(frames))
    return shots


def prepare():
    for c in candidates():
        key=c['id'];plan=PLANS[key];folder=OUT/key;media=folder/'media';media.mkdir(exist_ok=True)
        clips=[];offset=0;framings=[]
        source_words=[w for r in json.loads((folder/'context_transcript.json').read_text('utf-8'))
            if not re.search(r'субтитры|продолжение следует|девушки отдыхают|^конец$',r['text'],re.I) for w in r['words']]
        actual_segments=[]
        for i,(a,z) in enumerate(plan['segments']):
            for lo,hi,_ in source_words:
                if lo<a<hi:a=math.floor((lo-.12)*25)/25
                if lo<z<hi:z=math.ceil((hi+.12)*25)/25
            a=round(a,2);z=round(z,2);actual_segments.append([a,z])
            duration=round(z-a,2);file=media/f'part_{i:02}.mov';recipe=file.with_suffix('.recipe.json')
            config=dict(source=c['source'],start=a,end=z,wide=plan.get('wide',False),height=plan.get('height',1040),version=1)
            revision=1
            while file.exists() and (not recipe.exists() or json.loads(recipe.read_text('utf-8'))!=config):
                revision+=1;file=media/f'part_{i:02}_v{revision:02}.mov';recipe=file.with_suffix('.recipe.json')
            if not(file.exists() and recipe.exists() and json.loads(recipe.read_text('utf-8'))==config):
                shots=framing(c['source'],a,z,config['wide'],config['height']);n=len(shots)
                graph=[]
                if n>1:graph.append('[0:v]split='+str(n)+''.join(f'[s{k}]' for k in range(n)))
                for k,s in enumerate(shots):
                    h=round(1080*s['height']/s['width']/2)*2;y=(1200-h)//2
                    inp=f'[s{k}]' if n>1 else '[0:v]'
                    graph.append(inp+f"trim=start_frame={s['first']}:end_frame={s['last']},setpts=PTS-STARTPTS,"
                        f"crop={s['width']}:{s['height']}:{s['x']}:0,scale=1080:{h}:flags=lanczos,"
                        f'pad=1080:1200:0:{y}:black,setsar=1[v{k}]')
                graph.append(''.join(f'[v{k}]' for k in range(n))+f'concat=n={n}:v=1:a=0[v]')
                b.ffmpeg.run(['-ss',str(a),'-i',c['source'],'-t',str(duration),'-filter_complex_threads','1',
                    '-filter_complex',';'.join(graph),'-map','[v]','-map','0:a:0','-r','25','-frames:v',str(round(duration*25)),
                    '-c:v','libx264','-preset','fast','-crf','17','-pix_fmt','yuv420p','-c:a','pcm_s16le','-ac','2',
                    '-af',f'aresample=48000,afade=t=in:d=0.005,afade=t=out:st={duration-.005}:d=0.005',str(file)],timeout=300)
                b.save(recipe,config);b.save(file.with_suffix('.framing.json'),shots)
            framings.append(json.loads(file.with_suffix('.framing.json').read_text('utf-8')))
            clips.append(dict(file=file.resolve().as_posix(),start=round(offset,2),dur=duration,
                last_frame=(round(duration*25)-1)/25,voice=True,fx={},name=f'{a:.2f}–{z:.2f}'))
            offset=round(offset+duration,2)
        concat=media/'concat.txt';concat.write_text('\n'.join(f"file '{Path(c['file']).name}'" for c in clips),encoding='utf-8')
        clean=folder/'clean_cut.mp4'
        if not clean.exists() or any(Path(x['file']).stat().st_mtime>clean.stat().st_mtime for x in clips):
            b.ffmpeg.run(['-f','concat','-safe','0','-i',str(concat),'-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',str(clean)],timeout=120)
        base=json.loads((first.FOLDER/'timeline.json').read_text('utf-8'))
        title=plan.get('title',c['title'])
        data=dict(name=title,w=1080,h=1200,fps=25,total=offset,clips=clips,subs=[],flashes=[],music=None,
            plate=dict(lines=plan['plate'],font='Montserrat-SemiBold',size=36,dur=min(3.5,offset)),sub_style=base['sub_style'])
        timeline=folder/'timeline.json'
        old=json.loads(timeline.read_text('utf-8')) if timeline.exists() else {}
        if old.get('clips')==clips:data['subs']=old.get('subs',[])
        b.save(timeline,data)
        b.save(folder/'plan.json',dict(c,**{k:v for k,v in plan.items() if k!='title'},title=title,actual_segments=actual_segments,duration=offset,
            framing='Static within original shots; all detected faces retained; wide if ambiguous',upload_authorized=False))
        b.save(folder/'approval.json',dict(state='pending',upload_authorized=False))
        sheet=folder/'framing_sheet.jpg'
        if not sheet.exists() or sheet.stat().st_mtime<clean.stat().st_mtime:
            b.ffmpeg.run(['-i',str(clean),'-vf',f'fps=20/{offset},scale=270:-2,tile=4x5','-frames:v','1',str(sheet)],timeout=90)
        print('PREPARED',key,offset,'seconds',len(clips),'parts',flush=True)


def captions():
    model=b.whisper()
    for c in candidates():
        folder=OUT/c['id'];dest=folder/'edit_transcript.json'
        if dest.exists() and dest.stat().st_mtime>=(folder/'clean_cut.mp4').stat().st_mtime:rows=json.loads(dest.read_text('utf-8'))
        else:
            wav=folder/'media/edit.wav'
            b.ffmpeg.run(['-i',str(folder/'clean_cut.mp4'),'-vn','-ac','1','-ar','16000',str(wav)],timeout=90)
            segments,_=model.transcribe(str(wav),language='ru',word_timestamps=True,vad_filter=False,
                beam_size=5,condition_on_previous_text=False)
            rows=[dict(start=s.start,end=s.end,text=s.text,words=[[w.start,w.end,w.word.strip()] for w in s.words or []]) for s in segments]
            b.save(dest,rows)
        data=json.loads((folder/'timeline.json').read_text('utf-8'))
        rows=[r for r in rows if not re.search(r'субтитры (?:сделал|создал|добавил)|продолжение следует|девушки отдыхают|^\s*конец\s*$',r['text'],re.I)]
        words=b.subtitles._join_hyphens([b.subtitles.Word(*w) for r in rows for w in r['words'] if w[1]>w[0]])
        for w in words:
            if w.text in {'Эйда','Эда'}:w.text='Ада'
        data['subs']=b.captions(words,data['total']);b.save(folder/'timeline.json',data)
        (folder/'edit_dialogue.txt').write_text('\n'.join(f"{r['start']:.2f}–{r['end']:.2f} {r['text']}" for r in rows),encoding='utf-8')
        print('CAPTIONED',c['id'],len(words),len(data['subs']),flush=True)


def build():
    from editbot.ae.bridge import AfterEffects,find_ae,ae_running,TEMPLATE
    if ae_running():raise RuntimeError('AE is already open; do not close a user project')
    ae=AfterEffects(find_ae(),Path('C:/aebridge'),20,35)
    jobs=[];manifest=[]
    for c in candidates():
        folder=OUT/c['id'];data=json.loads((folder/'timeline.json').read_text('utf-8'))
        assert data['subs'] and len(data['clips'])
        fingerprint=hashlib.sha256((folder/'timeline.json').read_bytes()).hexdigest()
        receipt=folder/'build.json'
        if receipt.exists():
            prior=json.loads(receipt.read_text('utf-8'))
            if prior['timeline_sha256']==fingerprint and Path(prior['project']).exists():continue
        rev=1
        while (folder/f"{c['id']}_r{rev:02}.aep").exists():rev+=1
        stem=f"{c['id']}_r{rev:02}";project=folder/(stem+'.aep');raw=folder/'media'/(stem+'_ae.mp4')
        safe=ae.bridge/f's04_{uuid.uuid4().hex[:10]}.aep';err=safe.with_name(safe.stem+'_ERR.aep')
        data.update(aep=safe.as_posix(),err_aep=err.as_posix(),render_to=raw.resolve().as_posix(),quit=False)
        jobs.append(data);manifest.append(dict(key=c['id'],project=str(project.resolve()),safe=str(safe),error=str(err),
            raw=str(raw.resolve()),video=stem+'_music.mp4',revision=rev,timeline_sha256=fingerprint))
    if not jobs:print('All current projects already built.');return
    template=TEMPLATE.read_text('utf-8')
    code='var JOBS='+json.dumps(jobs,ensure_ascii=False)+';\n'
    code+='for(var j=0;j<JOBS.length;j++){(function(DATA){\n'+template+'\n})(JOBS[j]);}\n'
    code+='try {app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);}catch(e){} app.quit();'
    script=ae.bridge/f's04_batch_{uuid.uuid4().hex[:8]}.jsx';script.write_text(code,encoding='utf-8-sig')
    proc=subprocess.Popen([str(ae.dir/'AfterFX.exe'),'-r',str(script)])
    print('AE BUILD PID',proc.pid,'PROJECTS',len(jobs),flush=True)
    start=time.time();completed=set();last_saved=0
    while time.time()-start<1000:
        for row in manifest:
            if Path(row['error']).exists():raise RuntimeError('AE script error project: '+row['error'])
            if row['key'] not in completed and Path(row['safe']).exists():
                shutil.copy2(row['safe'],row['project']);b.save(OUT/row['key']/'build.json',row)
                completed.add(row['key']);last_saved=time.time();print('BUILT',row['key'],flush=True)
        if len(completed)==len(jobs) and (proc.poll() is not None or time.time()-last_saved>60):
            if proc.poll() is None:proc.kill();proc.wait(timeout=20)
            print('ALL AE PROJECTS BUILT',len(completed),flush=True);return
        if proc.poll() is not None and len(completed)!=len(jobs):raise RuntimeError('AE exited before all projects were saved')
        time.sleep(2)
    proc.kill();raise TimeoutError('AE did not complete batch project build')


def mapped_captions():
    """Keep native-source word timing across edit cuts; correct documented ASR homophones."""
    fixes={
        'P101':{'Возили':'Возились'},
        'P201':{'Туни.':'Томми.','Томи,':'Томми,','монеты.':'монетку.','парим,':'пари,'},
        'P202':{'Малет':'Малец','Малет,':'Малец,','силы':'сила'},
        'P203':{'жалоб.':'жалок.'},
        'P204':{'Поля':'Полли'},
        'P302':{'Лока,':'Лука,','Лока.':'Лука.','Лока!':'Лука!','Шпетнистый':'Пятнистый'},
        'P401':{'Погоди':'Погадай','Поли.':'Полли.','Поли,':'Полли,','чайники,':'чаинки,','чайники':'чаинки','чайнике.':'чаинки.','вылечай':'вылей чай'},
        'P402':{'пустое,':'пустого,'},
        'P403':{'Эйда':'Ада'},
        'P404':{'чьей':'в чьей'},
        'P405':{'пустыльку.':'пустельгу.','пустельку.':'пустельгу.','Альфи.':'Алфи.'},
        'P501':{'уважил':'уложил','уважила':'уложил','там':'Том'},
        'P504':{'пенсий,':'пенсов,','пенсий':'пенсов','пенсии,':'пенсов,','пирожный':'пирожное'},
        'P601':{'зузить':'сузить'},
        'P602':{'Матвеев.':'Маттео.','Матвео.':'Маттео.'},
        'P603':{'лично':'личных.','курорт.':'курок.','Анфи!':'Алфи!'},
    }
    for c in candidates():
        key=c['id'];folder=OUT/key;data=json.loads((folder/'timeline.json').read_text('utf-8'))
        plan=json.loads((folder/'plan.json').read_text('utf-8'))
        rows=json.loads((folder/'context_transcript.json').read_text('utf-8'))
        if key=='P504':
            # Both full-edit recognitions agree here; the 28-second source window
            # had split «я думал» into the spurious «яду» at its boundary.
            edited=json.loads((folder/'edit_transcript.json').read_text('utf-8'))
            a=plan['actual_segments'][0][0]
            rows=[dict(text=r['text'],words=[[w[0]+a,w[1]+a,w[2]] for w in r['words']]) for r in edited]
        words=[list(w) for row in rows if not re.search(r'субтитры|продолжение следует|девушки отдыхают|^конец$',row['text'],re.I) for w in row['words'] if w[1]>w[0]]
        corrections=[]
        if key=='P103':
            spot=json.loads((folder/'spotcheck.json').read_text('utf-8'))
            words=[w for w in words if not 2965.5<=w[0]<2970.7]+[list(w) for r in spot for w in r['words']]
        if key=='P602':
            words=[w for w in words if not 2061.6<=w[0]<2064.5]
            words += [[2061.84,2062.44,'мертвы,'],[2063.44,2063.76,'мистер'],[2063.76,2064.44,'Чангретта.']]
            corrections.append('Restore surname Чангретта and мертвы; compared original dialogue, edited ASR and short-window recognition.')
        for w in words:
            old=w[2];w[2]=fixes.get(key,{}).get(old,old)
            if key=='P601' and 568.1<w[0]<568.5:w[2]='И'
            if key=='P303' and 2820<w[0]<2823 and w[2] in {'О,','О','о,','о'}:w[2]='А'
            if old!=w[2]:corrections.append(dict(at=w[0],before=old,after=w[2]))
        words.sort(key=lambda w:w[0]);mapped=[]
        for clip,(a,z) in zip(data['clips'],plan['actual_segments']):
            for lo,hi,text in words:
                if not a<=((lo+hi)/2)<z:continue
                if key=='P102' and text.lower().strip('.,!?')=='узад':continue
                if key=='P201' and text.lower().strip('.,!?') in {'воль','только'}:continue
                if key=='P202' and 2553.8<=lo<2555.4:continue
                lo=max(0,lo-a)+clip['start'];hi=min(clip['dur'],hi-a)+clip['start']
                if hi>lo:mapped.append(b.subtitles.Word(round(lo,3),round(hi,3),text))
        mapped=b.subtitles._join_hyphens(mapped)
        joined=[];idx=0
        while idx<len(mapped):
            word=mapped[idx]
            if idx+1<len(mapped) and word.text.lower()=='не' and mapped[idx+1].text.lower().strip('.,!?')=='мало':
                joined.append(b.subtitles.Word(word.start,mapped[idx+1].end,'немало.'));idx+=2
            elif idx+1<len(mapped) and word.text.lower()=='нас' and mapped[idx+1].text.lower().strip('.,!?…')=='смотрятся':
                joined.append(b.subtitles.Word(word.start,mapped[idx+1].end,'насмотрятся...'));idx+=2
            elif idx+1<len(mapped) and word.text.lower().strip('.,!?…')=='две' and mapped[idx+1].text.lower().strip('.,!?…')=='двести':
                joined.append(b.subtitles.Word(word.start,mapped[idx+1].end,'двести'));idx+=2
            else:joined.append(word);idx+=1
        mapped=joined
        expanded=[]
        for word in mapped:
            parts=word.text.split()
            for j,token in enumerate(parts):
                step=(word.end-word.start)/len(parts)
                expanded.append(b.subtitles.Word(word.start+j*step,word.start+(j+1)*step,token))
        mapped=expanded
        # Resolve only demonstrated long-pause errors, using clean-edit and final-file
        # recognition together; do not blindly trust a short window's first-word timestamp.
        timing={
            'P102':{'кто':27.20,'всё':27.81},
            'P103':{'вот':1.44},
            'P203':{'вам':16.46,'следует':17.38,'разрядил':36.14,'совсем':46.50,'другого':47.40},
            'P204':{'как':.84},'P302':{'послушай':25.03},'P303':{'артур':22.79},
            'P601':{'тебе':28.39,'как':34.35,'помни':36.40},'P603':{'это':25.28},
        }.get(key,{})
        for word in mapped:
            norm=re.sub(r'[^а-яё]','',word.text.lower())
            if norm in timing and abs(timing[norm]-word.start)<4.5:
                new=timing[norm]
                corrections.append(dict(at=word.start,text=word.text,corrected_start=new,basis='Independent ASR comparison across the pause'))
                word.end=min(data['total'],new+min(.5,max(.12,word.end-word.start)));word.start=new
        clean_asr=folder/'edit_transcript.json';final_asr=folder/'rendered_transcript.json'
        if key!='P404' and clean_asr.exists() and final_asr.exists() and clean_asr.stat().st_mtime>=(folder/'clean_cut.mp4').stat().st_mtime:
            normalize=lambda s:re.sub(r'[^а-яё0-9]','',s.lower()).replace('ё','е')
            clean_words=[(w[0],normalize(w[2])) for r in json.loads(clean_asr.read_text('utf-8')) for w in r['words']]
            final_words=[(w[0],normalize(w[2])) for r in json.loads(final_asr.read_text('utf-8')) for w in r['words']]
            for word in mapped:
                token=normalize(word.text)
                if len(token)<3:continue
                one=[t for t,s in clean_words if s==token and abs(t-word.start)<2]
                two=[t for t,s in final_words if s==token and abs(t-word.start)<2]
                if len(one)==len(two)==1 and abs(one[0]-two[0])<=.25:
                    new=round((one[0]+two[0])/2,3)
                    if abs(new-word.start)>.3:
                        corrections.append(dict(at=word.start,text=word.text,corrected_start=new,basis='Two independent full-edit recognitions agree within 250 ms'))
                        word.end=min(data['total'],new+min(.5,max(.12,word.end-word.start)));word.start=new
        mapped.sort(key=lambda w:w.start)
        data['subs']=b.captions(mapped,data['total']);b.save(folder/'timeline.json',data)
        b.save(folder/'mapped_words.json',[[w.start,w.end,w.text] for w in mapped])
        b.save(folder/'caption_corrections.json',dict(basis='Native LostFilm source ASR; mapped by actual edit cuts; independent short-window and edited ASR review',corrections=corrections))
        print('SOURCE CAPTIONS',key,len(mapped),flush=True)


def render():
    from editbot.ae.bridge import AfterEffects,find_ae
    ae=AfterEffects(find_ae(),Path('C:/aebridge'),20,35)
    for c in candidates():
        folder=OUT/c['id'];build=json.loads((folder/'build.json').read_text('utf-8'))
        target=folder/(Path(build['video']).stem+'_mix2.mp4');raw=Path(build['raw'])
        if target.exists() and (folder/'latest.json').exists():
            prior=json.loads((folder/'latest.json').read_text('utf-8'))
            if prior.get('timeline_sha256')==build['timeline_sha256'] and prior.get('mix_version')==2:continue
        data=json.loads((folder/'timeline.json').read_text('utf-8'));duration=data['total']
        if not raw.exists():ae.render(Path(build['project']),raw,expected_duration=duration)
        else:
            receipt=json.loads(raw.with_suffix('.render.json').read_text('utf-8'))
            assert receipt['returncode']==0 and abs(receipt['duration']-duration)<.15
        voice=folder/'media'/(Path(build['video']).stem+'_voice.mp4')
        if not voice.exists():
            b.ffmpeg.run(['-i',str(raw),'-c:v','copy','-af','loudnorm=I=-16:TP=-3:LRA=9','-c:a','aac','-b:a','192k',str(voice)],timeout=120)
        mood=PLANS[c['id']]['mood'];prefix='jamendo_76bf7404' if mood in {'comic','warm'} else 'jamendo_4c8e6da4'
        music=next((ROOT/'music/auto').glob(prefix+'*.mp3'))
        bed=-21 if mood in {'comic','warm'} else (-22 if mood=='reflective' else -18)
        graph=('[0:a]aresample=48000,asplit=2[voice][key];[1:a]aresample=48000,'
            f'atrim=duration={duration},asetpts=PTS-STARTPTS,loudnorm=I=-18:TP=-3:LRA=9,aresample=48000,'
            f'highpass=f=70,lowpass=f=5500,volume={bed}dB,afade=t=in:d=0.6,'
            f'afade=t=out:st={duration-.8}:d=0.8[bed];'
            '[bed][key]sidechaincompress=threshold=0.008:ratio=8:attack=10:release=250[ducked];'
            '[voice][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.708:level=false:latency=true,'
            'loudnorm=I=-16:TP=-3:LRA=9,aresample=48000[a]')
        b.ffmpeg.run(['-i',str(voice),'-stream_loop','-1','-ss','20','-i',str(music),'-filter_complex',graph,
            '-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-b:a','192k','-t',str(duration),
            '-movflags','+faststart',str(target)],timeout=120)
        meta=json.loads(music.with_suffix('.json').read_text('utf-8'))
        b.save(folder/'music_credit.json',{k:meta.get(k) for k in ['title','creator','foreign_landing_url','license_url']})
        b.save(folder/'mix.json',dict(mood=mood,bed_db=bed,music_file=music.name,duck_under_speech=True,
            audition='Not continuously auditioned; local review pending'))
        b.save(folder/'latest.json',dict(video=target.name,project=Path(build['project']).name,revision=build['revision'],
            timeline_sha256=build['timeline_sha256'],mix_version=2))
        print('RENDERED',c['id'],duration,target.name,flush=True)


def candidates():
    return [c for c in json.loads(CATALOG.read_text(encoding='utf-8'))
            if c['id'].startswith('P') and c['grade']=='strong' and c['id']!='P301']


def study():
    OUT.mkdir(exist_ok=True);model=b.whisper()
    for c in candidates():
        folder=OUT/c['id'];folder.mkdir(exist_ok=True)
        start=c['start']-6 if c['id']!='P103' else 2840
        end=c['end']+6
        chunks=[]
        for i,a in enumerate(range(int(start),int(end),28)):
            z=min(a+28,end);dest=folder/f'context_{i:02}.json'
            if dest.exists():rows=json.loads(dest.read_text(encoding='utf-8'))
            else:
                wav=folder/f'context_{i:02}.wav'
                b.ffmpeg.run(['-ss',str(a),'-i',c['source'],'-t',str(z-a),'-map','0:a:0',
                    '-vn','-ac','1','-ar','16000',str(wav)],timeout=90)
                rows=first.recognize(model,wav,a);b.save(dest,rows)
            chunks+=rows
        b.save(folder/'context_transcript.json',chunks)
        (folder/'dialogue.txt').write_text('\n'.join(f"{r['start']:.2f}–{r['end']:.2f} {r['text']}" for r in chunks),encoding='utf-8')
        sheet=folder/'context_sheet.jpg'
        if not sheet.exists():
            b.ffmpeg.run(['-ss',str(start),'-i',c['source'],'-t',str(end-start),'-vf',
                f'fps=24/{end-start},scale=320:-2,tile=4x6','-frames:v','1',str(sheet)],timeout=120)
        print('STUDIED',c['id'],len(chunks),'phrases',flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace');logging.basicConfig(level=logging.INFO)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['study','prepare','captions','mapped_captions','build','render']);args=parser.parse_args()
    globals()[args.action]()
