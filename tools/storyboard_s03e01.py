from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot import ffmpeg
from PIL import Image,ImageDraw

source=next((ROOT/'library/Peaky.Blinders.S03.1080p.BluRay').glob('*S03E01*.mkv'))
out=ROOT/'review/s03e01'
for name,start,dur in [('wedding_dense',2730,76),('rules_dense',537,90)]:
    dest=out/f'{name}.jpg'
    ffmpeg.run(['-ss',str(start),'-i',str(source),'-t',str(dur),'-vf',
                'fps=1,scale=240:130,tile=8x12','-frames:v','1',str(dest)])
    img=Image.open(dest).copy()
    draw=ImageDraw.Draw(img)
    for i in range(dur):
        x,y=i%8*240,i//8*130
        draw.rectangle((x,y,x+55,y+17),fill='black')
        draw.text((x+3,y+2),str(start+i),fill='white')
    img.save(dest)
    print(dest,flush=True)
