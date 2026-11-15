from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def executable():
    configured = os.environ.get('EDITS_FFMPEG')
    if configured:
        if not Path(configured).is_file():
            raise ValueError('EDITS_FFMPEG does not point to a file')
        return configured
    binary = shutil.which('ffmpeg')
    if binary:
        return binary
    # Existing installation may supply FFmpeg without a second download.
    root = Path(__file__).resolve().parents[2]
    binaries = list((root / '.venv/Lib/site-packages/imageio_ffmpeg/binaries').glob('ffmpeg*.exe'))
    if binaries:
        return str(binaries[0])
    raise RuntimeError('Install FFmpeg or set EDITS_FFMPEG to its executable')


def run(arguments, cwd=None, timeout=3600):
    result = subprocess.run([executable(), '-hide_banner', '-nostdin', *map(str, arguments)],
                            cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError('FFmpeg failed: ' + result.stderr[-2000:])
    return result.stderr


def probe(path: Path):
    result = subprocess.run([executable(), '-hide_banner', '-i', str(path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    duration = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', result.stderr)
    video = re.search(r'Video:.*? (\d{2,5})x(\d{2,5}).*? ([\d.]+) fps', result.stderr)
    if not duration or not video:
        raise ValueError('Could not read video duration/dimensions/fps')
    return dict(duration=int(duration[1])*3600+int(duration[2])*60+float(duration[3]),
                width=int(video[1]), height=int(video[2]), fps=float(video[3]),
                has_audio='Audio:' in result.stderr)
