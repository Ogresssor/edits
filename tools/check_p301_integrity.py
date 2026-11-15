"""Check actual AE frames, continuous source audio and both local mixes."""
import hashlib
import json
import re
import subprocess
import numpy as np
import s04_first_edit as edit
import check_six_render_integrity as compare


def main():
    edit.select();folder=edit.FOLDER
    data=json.loads((folder/'timeline.json').read_text(encoding='utf-8'))
    variants=json.loads((folder/'variants.json').read_text(encoding='utf-8'))
    source=compare.gray(edit.Path(data['clips'][0]['file']))
    results=[];first=None
    for name in [variants['original'],variants['music']]:
        file=folder/name;info=edit.b.ffmpeg.probe(file)
        frames=compare.gray(file)
        assert len(frames)==len(source)==1090
        error=np.abs(frames[:,90:150,10:260].astype(np.float32)-source[:,90:150,10:260].astype(np.float32)).mean((1,2))
        assert np.percentile(error,95)<8
        if first is None:first=frames
        else:assert np.array_equal(first,frames),'Music mix changed video pictures'
        run=subprocess.run([edit.b.ffmpeg.ffmpeg_exe(),'-hide_banner','-i',str(file),'-vf',
            'blackdetect=d=0.01:pix_th=0.03:pic_th=0.99,freezedetect=n=-60dB:d=1',
            '-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
        assert run.returncode==0
        black=re.findall(r'black_start:[^\r\n]+',run.stderr)
        freeze=re.findall(r'freeze_start:[^\r\n]+',run.stderr)
        loudness=json.loads(run.stderr[run.stderr.rfind('{'):run.stderr.rfind('}')+1])
        assert not black and not freeze and float(loudness['input_tp'])<0
        audio=compare.audio_compare(file,data)
        assert abs(audio['parts'][0]['lag_ms'])<40
        assert audio['parts'][0]['median_correlation']>.9
        result=dict(file=name,sha256=hashlib.file_digest(file.open('rb'),'sha256').hexdigest(),
            frames=len(frames),expected_frames=1090,duration=info.duration,fps=info.fps,
            p95_frame_error=round(float(np.percentile(error,95)),4),full_decode=True,
            black_events=black,freeze_flags=freeze,loudness=loudness,audio_comparison=audio)
        results.append(result)
        print(name,json.dumps(result,ensure_ascii=False),flush=True)
    words=[w for r in json.loads((folder/'context_transcript.json').read_text(encoding='utf-8')) for w in r['words']]
    words += [w for r in json.loads((folder/'context_450.json').read_text(encoding='utf-8')) for w in r['words']]
    edges=dict(start=edit.START,end=edit.END,
        start_inside_word=[w for w in words if w[0]<edit.START<w[1]],
        end_inside_word=[w for w in words if w[0]<edit.END<w[1]])
    assert not edges['start_inside_word'] and not edges['end_inside_word']
    original=edit.Path(variants['original'])
    receipt=json.loads((folder/'media'/(original.stem+'_ae.render.json')).read_text(encoding='utf-8'))
    assert receipt['returncode']==0 and abs(receipt['duration']-43.6)<.15
    edit.b.save(folder/'integrity.json',dict(variants=results,speech_edges=edges,ae_render=receipt,
        music_video_identical=True,audio_cut_count=0,approval='pending',upload_authorized=False))
    print('INTEGRITY PASSED; 2 local variants, no publication',flush=True)


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8',errors='replace');main()
