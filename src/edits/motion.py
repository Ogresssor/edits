"""Explicit, bounded scene accents. No automatic/random effect placement."""
import math

VERSION = 'scene-motion-2026-11-15.1'


def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def validate(plan, duration):
    if plan.get('text_motion','none') not in ('none','pop','reveal'):
        raise ValueError('text_motion must be none, pop or reveal')
    for caption in plan.get('captions',[]):
        words=caption.get('words',[])
        if plan.get('text_motion')=='reveal' and not words:
            raise ValueError('Word reveal requires actual word timestamps')
        previous=caption['start']
        for word in words:
            from .design import plain
            plain(word.get('text'))
            start,end=word.get('start'),word.get('end')
            if not (number(start) and number(end) and previous<=start<end<=caption['end']+.001):
                raise ValueError('Invalid word timestamps')
            previous=end
        if words and ' '.join(w['text'] for w in words)!=caption['text']:
            raise ValueError('Word timestamps must match the exact caption text')
    effects=plan.get('scene_effects',[])
    if not isinstance(effects,list) or len(effects)>8:
        raise ValueError('Use at most eight explicit scene accents')
    previous=0
    for effect in effects:
        start,end=effect.get('start'),effect.get('end')
        if not (number(start) and number(end) and previous<=start<end<=duration and end-start>=.3):
            raise ValueError('Scene accents must be ordered, non-overlapping and inside the edit')
        if not str(effect.get('reason','')).strip():
            raise ValueError('Scene accent requires an editorial reason')
        kind=effect.get('kind')
        if kind=='punch_in':
            amount=effect.get('amount',.055)
            if not number(amount) or not 0<amount<=.08:
                raise ValueError('Punch-in is limited to eight percent')
        elif kind=='focus':
            box=effect.get('box',[])
            if (not isinstance(box,list) or len(box)!=4 or not all(number(n) for n in box)
                    or not (0<=box[0]<1 and 0<=box[1]<1 and box[2]>0 and box[3]>0
                            and box[0]+box[2]<=1 and box[1]+box[3]<=1)):
                raise ValueError('Focus box must fit inside normalized output coordinates')
            if not .2<=end-start<=2.5:
                raise ValueError('Focus must be brief: up to 2.5 seconds')
        else:
            raise ValueError('Unsupported scene accent')
        previous=end


def video_filters(effects, width, height, fps):
    filters=[];current='vjoin'
    for index,effect in enumerate(effects):
        out=f'motion{index}';start,end=effect['start'],effect['end']
        if effect['kind']=='punch_in':
            first,last=start*fps,end*fps;ramp=max(2,.14*fps)
            envelope=f'min(1,max(0,(on-{first:.4f})/{ramp:.4f}))*min(1,max(0,({last:.4f}-on)/{ramp:.4f}))'
            z=f'1+{effect.get("amount",.055):.5f}*({envelope})'
            filters.append(f"[{current}]zoompan=z='{z}':x=iw/2-iw/zoom/2:y=ih/2-ih/zoom/2:d=1:s={width}x{height}:fps={fps}[{out}]")
        else:
            box=effect['box'];x=round(box[0]*width/2)*2;y=round(box[1]*height/2)*2
            w=max(2,round(box[2]*width/2)*2);h=max(2,round(box[3]*height/2)*2)
            w=min(w,width-x);h=min(h,height-y)
            enabled=f'between(t,{start:.4f},{end:.4f})'
            filters += [f'[{current}]split=3[orig{index}][blur{index}][detail{index}]',
                        f'[blur{index}]gblur=sigma=7,eq=brightness=-0.10,format=yuva420p,fade=t=in:st={start}:d=0.14:alpha=1,fade=t=out:st={end-.14:.4f}:d=0.14:alpha=1[wash{index}]',
                        f"[orig{index}][wash{index}]overlay=0:0:enable='{enabled}'[dim{index}]",
                        f'[detail{index}]crop={w}:{h}:{x}:{y}[roi{index}]',
                        f"[dim{index}][roi{index}]overlay={x}:{y}:enable='{enabled}'[{out}]"]
        current=out
    return filters,current
