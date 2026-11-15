"""Обработка музыки для эдита: «slowed + reverb», бит и «дроп» (место, где трек взрывается).

Эталонные эдиты сделаны на замедленных трендовых треках с эхом. Нам можно только свободную музыку
(CC0/CC BY), поэтому этот звук делаем сами: трек замедляется (тон ниже, темп медленнее), добавляется
эхо-реверберация. Потом по обработанному треку ищем доли и дроп — резкий рост громкости/энергии.
Дроп ставится ровно на начало финальной нарезки эдита.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ffmpeg

log = logging.getLogger("musicfx")

SR = 22050


@dataclass
class MusicFx:
    path: Path                 # обработанный трек (wav 48 кГц)
    duration: float
    beats: np.ndarray          # доли, секунды от начала обработанного трека
    period: float              # средняя длина доли
    drops: list[tuple[float, float]]   # (время, сила) — кандидаты на дроп, по убыванию силы

    def beats_in(self, offset: float, total: float) -> np.ndarray:
        """Доли на шкале ролика (музыка начинается с offset)."""
        b = self.beats - offset
        return b[(b >= -0.01) & (b <= total + 0.01)]


def prepare(src: Path, work: Path, slow: float = 0.88, reverb: bool = True) -> MusicFx:
    out = work / "music_fx.wav"
    chain = ["aresample=44100"]
    if abs(slow - 1.0) > 0.005:
        chain.append(f"asetrate={int(44100 * slow)}")          # замедление вместе с тоном — как в «slowed»
    chain.append("aresample=48000")
    if reverb:
        # мягкое эхо: несколько затухающих отражений + чуть приглушённый верх, как у «reverb»-версий
        chain.append("aecho=0.85:0.75:45|95|160|240:0.22|0.16|0.11|0.07,lowpass=f=13500")
    ffmpeg.run(["-i", str(src), "-vn", "-af", ",".join(chain), "-ac", "2", "-c:a", "pcm_s16le", str(out)])

    import librosa

    proc = ffmpeg.popen_raw(["-i", str(out), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"])
    assert proc.stdout
    y = np.frombuffer(proc.stdout.read(), np.float32)
    proc.wait()
    dur = len(y) / SR
    _, beats = librosa.beat.beat_track(y=y, sr=SR, units="time", tightness=120)
    beats = np.asarray(beats, float)
    period = float(np.median(np.diff(beats))) if len(beats) > 4 else 0.5

    # дроп: энергия в следующие 4 с заметно выше, чем в предыдущие 4 с (+ сильный онсет в этой точке)
    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    fps = SR / hop
    rms_db = 20 * np.log10(rms + 1e-5)
    w = int(4 * fps)
    nov = np.full(len(rms_db), -99.0)
    for i in range(w, len(rms_db) - w):
        nov[i] = rms_db[i:i + w].mean() - rms_db[i - w:i].mean()
    onset = librosa.onset.onset_strength(y=y, sr=SR, hop_length=hop)
    onset = onset / (np.percentile(onset, 99) + 1e-6)
    score = nov + 2.0 * onset[: len(nov)]
    drops: list[tuple[float, float]] = []
    order = np.argsort(-score)
    for i in order[:400]:
        t = i / fps
        if score[i] < 1.0:
            break
        if all(abs(t - d) > 6 for d, _ in drops):
            if len(beats):                                    # точно на долю
                t = float(beats[np.argmin(np.abs(beats - t))])
            drops.append((t, float(score[i])))
        if len(drops) >= 8:
            break
    log.info("Музыка: slowed x%.2f%s, %.0f BPM, %.0f с, дропы: %s", slow, " + reverb" if reverb else "",
             60 / period, dur, ", ".join(f"{t:.1f} с ({s:.1f})" for t, s in drops[:4]) or "нет")
    return MusicFx(out, dur, beats, period, drops)


def choose_offset(fx: MusicFx, drop_at: float, total: float) -> float:
    """С какого места трека начинать, чтобы дроп пришёлся на drop_at секунду ролика."""
    for t, _ in fx.drops:
        off = t - drop_at
        if off >= 0 and t + (total - drop_at) <= fx.duration - 0.5:
            return off
    # дропа в нужном месте нет — самый громкий кусок нужной длины
    return max(0.0, min(fx.duration - total, fx.duration * 0.3))
