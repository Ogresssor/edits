"""Тонкая обёртка над ffmpeg: поиск бинарника, probe, запуск команд."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("ffmpeg")


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


class FFmpegError(RuntimeError):
    pass


def run(args: list[str], cwd: Path | None = None, timeout: float | None = None) -> None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg завершился с кодом {proc.returncode}:\n{proc.stderr[-3000:]}")


def popen_raw(args: list[str]) -> subprocess.Popen:
    """Запускает ffmpeg, который пишет сырые данные в stdout."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", *map(str, args)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 20)


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    audio_langs: list[str] = field(default_factory=list)  # метки языка дорожек ("rus", "eng" или "")
    audio_index: int = 0  # какую звуковую дорожку брать (выбирает аналитик)
    audio_titles: list[str] = field(default_factory=list)  # названия дорожек («Дубляж - Ист-Вест», …)


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\S+.*?Video:.*?(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def probe(path: Path) -> MediaInfo:
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = proc.stderr
    m = _DURATION.search(out)
    if not m:
        raise FFmpegError(f"не удалось определить длительность: {path}\n{out[-1500:]}")
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    width = height = 0
    fps = 25.0
    for line in out.splitlines():
        if "Video:" in line and "attached pic" not in line:
            v = _VIDEO.search(line)
            if v:
                width, height = int(v[1]), int(v[2])
            f = _FPS.search(line)
            if f:
                fps = float(f[1])
            break
    audio_langs, audio_titles, cur = [], [], None
    for line in out.splitlines():
        a = re.search(r"Stream #\d+:\d+(?:\[\w+\])?(?:\((\w+)\))?: (\w+):", line)
        if a:
            cur = a[2] == "Audio"
            if cur:
                audio_langs.append((a[1] or "").lower())
                audio_titles.append("")
        elif cur and audio_titles and not audio_titles[-1]:
            t = re.match(r"\s+title\s*:\s*(.*)", line)
            if t:
                audio_titles[-1] = t[1].strip()
    return MediaInfo(duration, width, height, fps, bool(audio_langs), audio_langs, audio_titles=audio_titles)


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"
