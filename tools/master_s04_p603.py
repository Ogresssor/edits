"""Give the gunshots AAC true-peak headroom without re-rendering video."""
import json
import s04_season_edit as e

def main():
    folder=e.OUT/'P603';latest=json.loads((folder/'latest.json').read_text('utf-8'))
    if latest.get('audio_mastered'):return
    source=folder/latest['video'];target=source.with_name(source.stem+'_master.mp4')
    if target.exists():raise FileExistsError(target)
    e.b.ffmpeg.run(['-i',str(source),'-map','0:v:0','-map','0:a:0','-c:v','copy',
        '-af','loudnorm=I=-16:TP=-3:LRA=9,aresample=48000','-c:a','aac','-b:a','192k',
        '-t','46.8','-movflags','+faststart',str(target)],timeout=120)
    e.b.save(folder/'audio_master.json',dict(source=source.name,output=target.name,
        reason='Decoded AAC reconstruction reached +0.03 dBTP at a gunshot; preserve headroom',
        loudness_target=-16,true_peak_target=-3,video_stream_copied=True))
    latest.update(video=target.name,audio_mastered=True);e.b.save(folder/'latest.json',latest)
    print('MASTERED',target.name)

if __name__=='__main__':main()
