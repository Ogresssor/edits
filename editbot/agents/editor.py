"""Агент-монтажёр: собирает ролик через ffmpeg.

  * scene   — одна цельная сцена: границы подгоняются так, чтобы не резать реплику, звук фильма главный,
              музыка — тихая подложка, субтитры;
  * montage — общий эдит: нарезка пиковых моментов под бит музыки.

Оба вида — на весь экран 9:16: окно кадрирования следует за лицами и движением (editbot/reframe.py).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import burned, ffmpeg, interp, musicfx, reframe, subtitles, upscale, watermark
from ..config import Config
from .analyzer import Analysis, Moment
from .curator import EditPlan
from .music import Beats, Track
from .trends import Movie

log = logging.getLogger("editor")


# ---------------------------------------------------------------- планирование нарезки общего эдита
@dataclass
class Clip:
    moment: Moment
    src_start: float     # откуда брать в фильме
    out_dur: float       # длительность в эдите
    speed: float         # 1.0 или slow-mo
    flash: bool


def plan_clips(plan: EditPlan, beats: Beats | None, cfg: Config) -> list[Clip]:
    e = cfg.edit
    target, mn, mx = float(e.montage_duration), float(e.min_clip), float(e.max_clip)
    period = beats.period if beats else 0.5
    # варианты длительностей в долях — так склейки попадают в бит
    options = sorted({k for k in (1, 2, 3, 4, 6, 8) if mn - 0.01 <= k * period <= mx + 0.01}) or [
        max(1, round(mn / period))]

    def length_for(progress: float) -> float:
        # кривая темпа: вступление длинными кадрами -> разгон -> быстрая нарезка -> финальный длинный кадр
        if progress < 0.15:
            want = mx
        elif progress < 0.45:
            want = (mx + mn) / 2
        elif progress < 0.9:
            want = mn
        else:
            want = mx
        k = min(options, key=lambda b: abs(b * period - want))
        return k * period

    moments = list(plan.moments)
    clips: list[Clip] = []
    used: dict[int, float] = {}  # докуда момент уже израсходован
    total, idx = 0.0, 0
    while total < target - period * 0.5 and moments:
        m = moments[idx % len(moments)]
        progress = total / target
        dur = min(length_for(progress), max(period, target - total))
        speed = float(e.slowmo_speed) if (m.slowmo and dur >= 1.5 * period) else 1.0
        span = dur * speed
        # первое использование — от пика, повторное — соседний кусок той же сцены
        start = m.peak - span * 0.35 if m.id not in used else used[m.id]
        lo, hi = m.start, max(m.start, m.end - span)
        if start > hi + 4:  # момент исчерпан — переходим к следующему
            moments.remove(m)
            continue
        start = min(max(start, lo - 1.0), hi + 2.0)
        used[m.id] = start + span
        flash = bool(e.flash_transitions) and clips and (len(clips) % 4 == 0 or progress > 0.9)
        clips.append(Clip(m, max(0.0, start), dur, speed, bool(flash)))
        total += dur
        idx += 1
    return clips


# ---------------------------------------------------------------- вспомогательное
def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:3]


def safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")[:60] or "edit"


class EditorAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.e = cfg.edit
        if self.e.format == "vertical":
            self.W, self.H = 1080, 1920
        else:
            self.W, self.H = 1920, 1080
        self.fps = int(self.e.fps)

    # ---------------------------------------------------------------- один кусок фильма
    safe_mode = False   # повторная попытка после ошибки: без нейросети и без тяжёлых эффектов
    _lowq = False       # источник низкого качества (меньше 500 строк: DVD-рипы и т. п.)

    def _upscaler(self):
        if self.safe_mode or str(self.e.get("upscale", "ai")) != "ai":
            return None
        # слабый рип (DVD/XviD): нейросеть сильнее давит шум и блоки сжатия, иначе вытягивает мусор
        dn = float(self.e.get("upscale_denoise_lowq", 0.65)) if self._lowq else float(self.e.get("upscale_denoise", 0.3))
        return upscale.get(dn)

    def _decode(self, movie: Path, info: ffmpeg.MediaInfo, start: float, span: float) -> np.ndarray:
        """Кадры куска фильма (RGB) с постоянной частотой исходника."""
        w, h = info.width, info.height
        # слабый рип: до увеличения убираем блочность сжатия и мелкий шум
        clean = ["-vf", "deblock=filter=strong:block=8,hqdn3d=1.2:1.2:5:5"] if self._lowq else []
        proc = ffmpeg.popen_raw(["-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", movie, "-map", "0:v:0", "-an",
                                 *clean, "-fps_mode", "cfr", "-r", f"{info.fps:.5f}",
                                 "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        assert proc.stdout
        data = proc.stdout.read()
        proc.wait()
        n = len(data) // (w * h * 3)
        if n == 0:
            raise RuntimeError(f"не удалось декодировать кусок {ffmpeg.fmt_time(start)}")
        return np.frombuffer(data[: n * w * h * 3], np.uint8).reshape(n, h, w, 3)

    def _segment(self, movie: Path, info: ffmpeg.MediaInfo, src_start: float, out_dur: float, speed: float,
                 flash: bool, zoom: bool, out: Path, soft_audio: bool = False) -> None:
        """Вырезает кусок, кадрирует 9:16 по главному герою (по кадрам исходника), увеличивает
        нейросетью и красит."""
        W, H, fps, e = self.W, self.H, self.fps, self.e
        movie = Path(movie).resolve()  # ffmpeg запускается из рабочей папки ролика
        span = out_dur * speed + 0.2
        src_start = max(0.0, min(src_start, info.duration - span - 0.3))

        frames = self._decode(movie, info, src_start, span)
        framer = reframe.Framer(info.width, info.height, W, H, self._upscaler())
        mode = str(e.get("reframe", "smart"))
        try:
            centers, cuts = (reframe.analyze_frames(frames, info.fps) if mode != "center"
                             else (np.full(len(frames), np.nan), np.zeros(len(frames), bool)))
            path = reframe.plan_path(centers, cuts, info.fps, framer.half, mode)
        except Exception as ex:  # кадрирование не должно ронять монтаж
            log.warning("Кадрирование: анализ не удался (%s), беру центр", ex)
            path = np.full(len(frames), 0.5)

        f = ["setpts=PTS-STARTPTS"]
        if speed != 1.0:
            f.append(f"setpts=PTS/{speed}")
        f.append(f"fps={fps}")
        if zoom:
            n_out = max(1, int(out_dur * fps))
            z = 0.10 if speed != 1.0 else 0.06
            f.append(f"scale={int(W * 1.5) // 2 * 2}:{int(H * 1.5) // 2 * 2}:flags=lanczos")
            f.append(f"zoompan=z='1+{z}*on/{n_out}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                     f":s={W}x{H}:fps={fps}")
        if e.grade:  # мягкая цветокоррекция: чуть контраста и цвета, лёгкая виньетка
            f.append("eq=contrast=1.04:saturation=1.08")
            f.append("vignette=PI/7")
        if framer.up is None and framer.k > 1.7:
            f.append("unsharp=5:5:0.5")   # без нейросети — немного резкости после растяжения
        if flash:
            f.append("fade=t=in:st=0:d=0.15:color=white")
        f.append("setsar=1,format=yuv420p")
        vf = "[0:v]" + ",".join(f) + "[v]"

        if info.has_audio:
            # на стыках кусков истории — короткие фейды звука, чтобы не было щелчков
            soft = (f",afade=t=in:d=0.08,afade=t=out:st={max(0.0, out_dur - 0.12):.3f}:d=0.12"
                    if soft_audio else "")
            af = (f"[1:a:{info.audio_index}]asetpts=PTS-STARTPTS,atempo={speed},aresample=48000,"
                  f"aformat=channel_layouts=stereo{soft},apad[a]")
            extra_in: list = []
        else:
            af = "[2:a]aformat=channel_layouts=stereo[a]"
            extra_in = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd = [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-framerate", f"{info.fps:.5f}", "-i", "-",
               "-ss", f"{src_start:.3f}", "-t", f"{span:.3f}", "-i", str(movie), *extra_in,
               "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
               "-t", f"{out_dur:.3f}", "-r", str(fps),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out.name]
        err = out.with_suffix(".log")
        with open(err, "wb") as errf:
            proc = subprocess.Popen(cmd, cwd=out.parent, stdin=subprocess.PIPE, stderr=errf)
            assert proc.stdin
            try:
                for i in range(0, len(frames), 16):
                    proc.stdin.write(framer(frames[i:i + 16], path[i:i + 16]).tobytes())
            except BrokenPipeError:
                pass
            proc.stdin.close()
            code = proc.wait()
        if code != 0:
            raise ffmpeg.FFmpegError(f"ffmpeg завершился с кодом {code}:\n"
                                     f"{err.read_text(encoding='utf-8', errors='replace')[-3000:]}")

    # ---------------------------------------------------------------- ролики
    def render(self, movie: Movie, an: Analysis, plan: EditPlan, index: int, out_file: Path,
               track: Track | None) -> Path:
        self._wm = None
        self._lowq = an.info.height < int(self.e.get("lowq_height", 500))
        if self._lowq:
            log.info("Источник низкого разрешения (%dx%d): убираю блочность и шум перед увеличением",
                     an.info.width, an.info.height)
        if self.e.get("remove_watermark", True):
            try:
                self._wm = watermark.detect(movie.path, an.info, an.workdir)
            except Exception as ex:
                log.warning("Поиск вотермарки не удался (%s)", ex)
        # холст: «arc» — почти квадрат во весь кадр (как у эталонных эдитов), остальные — 9:16
        if plan.kind == "arc":
            self.W, self.H = (int(x) for x in str(self._arc().get("canvas", "1440x1600")).lower().split("x"))
        elif self.e.format == "vertical":
            self.W, self.H = 1080, 1920
        else:
            self.W, self.H = 1920, 1080
        self._music = None
        self._ae_media = None
        if plan.kind == "arc" and self._ae_on():
            # куски для After Effects лежат рядом с проектом .aep (проект можно открыть и поправить руками)
            self._ae_media = out_file.parent / "ae" / f"{out_file.stem}_media"
            self._ae_media.mkdir(parents=True, exist_ok=True)
        work = an.workdir / f"render_{index}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        if plan.kind == "arc":
            total, ass = self._build_arc(movie, an, plan, work, track)
        elif plan.kind == "scene":
            total, ass = self._build_scene(movie, an, plan, work, track)
        elif plan.kind == "edit":
            total, ass = self._build_edit(movie, an, plan, work, track)
        elif plan.kind == "story":
            total, ass = self._build_story(movie, an, plan, work)
        else:
            total, ass = self._build_montage(movie, an, plan, work, track)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        if self._ae_media is not None:
            self._finalize_ae(out_file, plan)
            log.info("Готово: %s", out_file)
            return out_file
        self._finalize(work, track, total, plan, ass, out_file, kind=plan.kind)
        log.info("Готово: %s", out_file)
        return out_file

    @staticmethod
    def _snap_to_cut(t: float, cuts: list[float], words: list[subtitles.Word], side: str) -> float:
        """Если рядом склейка и на ней никто не говорит — ставим границу ровно на склейку (чистый монтаж)."""
        near = [c for c in cuts if abs(c - t) <= 0.8]
        if not near:
            return t
        c = min(near, key=lambda x: abs(x - t))
        # склейка в короткой паузе между словами одной фразы — всё равно «посреди речи»
        if any(w.start - 0.35 < c < w.end + 0.35 for w in words):
            return t
        return c + 0.05 if side == "start" else c - 0.05

    def _build_story(self, movie: Movie, an: Analysis, plan: EditPlan, work: Path) -> tuple[float, bool]:
        max_total = float(self.e.scene_max_sec) + 3
        subs_on = bool(self.cfg.subtitles.enabled)
        parts: list[tuple[float, float, list[subtitles.Word]]] = []
        for s, e in plan.segments:
            words = subtitles.transcribe(self.cfg, movie.path, max(0.0, s - 3), e - s + 6, work,
                                         an.info.audio_index) if subs_on else []
            s, e = subtitles.snap_window(words, s, e, e - s + 6)
            s = self._snap_to_cut(s, an.cuts, words, "start")
            e = self._snap_to_cut(e, an.cuts, words, "end")
            e = min(e, an.info.duration - 0.3)
            if e - s >= 0.8:
                parts.append((s, e, words))
        if not parts:
            raise RuntimeError("пустая история: ни одного куска после подгонки границ")
        # слишком длинно — подрезаем начало первого куска (развязку в конце не трогаем)
        excess = sum(e - s for s, e, _ in parts) - max_total
        if excess > 0:
            s, e, w = parts[0]
            cut = min(s + excess, e - 3)
            # не посреди фразы: начинаем с ближайшего начала предложения после точки подрезки
            starts = [x.start - 0.15 for i, x in enumerate(w)
                      if x.start >= cut and (i == 0 or w[i - 1].text.endswith((".", "!", "?", "…")))
                      and x.start < e - 3]
            parts[0] = (starts[0] if starts else cut, e, w)

        names, out_words, out_srt, offset = [], [], [], 0.0
        for i, (s, e, words) in enumerate(parts):
            dur = round(e - s, 3)
            log.info("  кусок %d: %s–%s (%.1f с)", i + 1, ffmpeg.fmt_time(s), ffmpeg.fmt_time(e), dur)
            out = work / f"part_{i}.mp4"
            self._segment(movie.path, an.info, s, dur, 1.0, False, bool(self.e.scene_zoom), out,
                          soft_audio=len(parts) > 1)
            names.append(f"file '{out.name}'")
            # слова и строки .srt — на общую шкалу времени ролика
            out_words += [subtitles.Word(w.start - s + offset, w.end - s + offset, w.text)
                          for w in words if s - 0.1 <= w.start and w.end <= e + 0.1]
            out_srt += [(a - s + offset, b - s + offset, t) for a, b, t in subtitles.srt_words(an.subs, s, e)]
            offset += dur
        if len(names) == 1:
            (work / "part_0.mp4").rename(work / "joined.mp4")
        else:
            (work / "list.txt").write_text("\n".join(names), encoding="utf-8")
            ffmpeg.run(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=work)
        total = round(offset, 3)
        log.info("История «%s»: %d кусок(а), %.1f с — %s", plan.theme, len(parts), total, plan.thought)

        ass = False
        if subs_on:
            n = subtitles.build_ass(self.cfg, out_words, out_srt, 0.0, total, self.W, self.H, work / "subs.ass")
            ass = n > 0
            log.info("  субтитры: %d фраз", n)
        return total, ass

    def _build_montage(self, movie: Movie, an: Analysis, plan: EditPlan, work: Path,
                       track: Track | None) -> tuple[float, bool]:
        clips = plan_clips(plan, track.beats if track else None, self.cfg)
        if not clips:
            raise RuntimeError("не из чего монтировать: пустой список клипов")
        fps, _ = self._out_fps(an.info)
        names, t, f_prev = [], 0.0, 0
        for i, clip in enumerate(clips):
            t += clip.out_dur
            f_next = int(round(t * fps))                     # склейки по кадрам на общей шкале
            n_frames, f_prev = f_next - f_prev, f_next
            out = work / f"clip_{i:03d}.mkv"
            fx = {"punch": 0.12, "push": 0.05, "shake": 16 if clip.flash else 0, "flash": clip.flash}
            self._fragment(movie.path, an.info, clip.src_start, n_frames,
                           "slowmo" if clip.speed < 1 else "", False, fx, out)
            names.append(f"file '{out.name}'")
        total = round(f_prev / fps, 3)
        log.info("Общий эдит «%s»: %d склеек, %.1f с", plan.theme, len(clips), total)
        (work / "list.txt").write_text("\n".join(names), encoding="utf-8")
        ffmpeg.run(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"], cwd=work)
        return total, False

    # ---------------------------------------------------------------- эдит: короткие фрагменты под бит
    def _beat_unit(self, track: Track | None) -> float:
        """Шаг сетки склеек: одна доля трека (при быстром темпе — две), 0.4–1.0 с."""
        p = track.beats.period if track else 0.5
        while p < 0.4:
            p *= 2
        while p > 1.0:
            p /= 2
        return p

    @staticmethod
    def _speed_curve(fx: str, n: int, fps: int) -> np.ndarray:
        """Скорость воспроизведения для каждого кадра результата."""
        t = np.arange(n) / max(1, n - 1)
        if fx == "slowmo":
            return np.full(n, 0.5)
        if fx == "ramp":
            # speed ramp: обычная скорость -> резкое замедление в середине движения -> чуть разгоняемся к концу
            ease = lambda u: np.clip(u, 0, 1) ** 2 * (3 - 2 * np.clip(u, 0, 1))
            down = 1.0 - 0.72 * ease((t - 0.30) / 0.15)
            up = 0.45 * ease((t - 0.80) / 0.20)
            return np.clip(down + up, 0.28, 1.2)
        if fx == "fast":
            return np.full(n, 1.5)
        return np.ones(n)

    def _effects(self, img: np.ndarray, t: float, dur: float, fx: dict, since_cut: float = 99.0) -> np.ndarray:
        """Динамика кадра: «удар» наездом на склейке, медленный наезд, тряска, вспышка и переходы
        (whip — рывок с размытием движения, zoomblur — радиальное размытие, rgb — сдвиг цветовых каналов)."""
        import cv2

        H, W = img.shape[:2]
        z = 1.0 + fx.get("push", 0.0) * (t / max(dur, 1e-3))             # медленный наезд через фрагмент
        if t < 0.28:                                                     # «удар» на склейке фрагмента
            z *= 1.0 + fx.get("punch", 0.0) * (1 - t / 0.28) ** 3
        if fx.get("cut_punch") and since_cut < 0.25:                     # и на родных склейках сцены
            z *= 1.0 + fx["cut_punch"] * (1 - since_cut / 0.25) ** 3
        dx = dy = 0.0
        if fx.get("shake") and t < 0.32:                                 # затухающая тряска
            a = fx["shake"] * (1 - t / 0.32)
            dx, dy = a * np.sin(t * 95.0), a * 0.7 * np.cos(t * 77.0)
        if z > 1.0005 or dx or dy:
            m = np.array([[z, 0, (1 - z) * W / 2 + dx], [0, z, (1 - z) * H / 2 + dy]], np.float32)
            img = cv2.warpAffine(img, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        tr = fx.get("transition", "")
        win = 0.16                                                       # длительность перехода после склейки
        if tr and t < win:
            k = 1 - t / win                                              # сила: 1 на склейке -> 0
            if tr == "whip":
                n = int(6 + 90 * k)                                      # размытие движения по горизонтали
                kernel = np.zeros((1, n), np.float32)
                kernel[0, :] = 1.0 / n
                img = cv2.filter2D(img, -1, kernel)
                shift = int(80 * k * (1 if fx.get("dir", 1) > 0 else -1))
                img = np.roll(img, shift, axis=1)
            elif tr == "zoomblur":                                       # радиальное размытие: среднее зумов
                acc = img.astype(np.float32)
                steps = 5
                for s_ in range(1, steps + 1):
                    zz = 1 + 0.06 * s_ * k
                    m = np.array([[zz, 0, (1 - zz) * W / 2], [0, zz, (1 - zz) * H / 2]], np.float32)
                    acc += cv2.warpAffine(img, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
                img = (acc / (steps + 1)).astype(np.uint8)
            elif tr == "rgb":                                            # сдвиг каналов (хроматическая аберрация)
                sh = int(4 + 26 * k)
                img = img.copy()
                img[:, :, 0] = np.roll(img[:, :, 0], sh, axis=1)
                img[:, :, 2] = np.roll(img[:, :, 2], -sh, axis=1)
        if (fx.get("flash") or tr == "flash") and t < 0.13:               # белая вспышка на сильной доле
            k = 0.75 * (1 - t / 0.13)
            img = cv2.addWeighted(img, 1 - k, np.full_like(img, 255), k, 0)
        return img

    _layout_cached: tuple[int, int] = (1920, 0)
    _wm = None

    def _layout(self, info: ffmpeg.MediaInfo) -> tuple[int, int]:
        """Высота окна с видео и отступ сверху. edit.zoom_pct — масштаб относительно «фильм во всю ширину»:
        100 = весь кадр фильма, 150 = чуть ближе (по бокам срезано ~1/3), ≥ ~315 = видео на весь экран."""
        z = max(100.0, float(self.e.get("zoom_pct", 150))) / 100
        area_h = int(round(self.W * info.height / (info.width / z) / 2)) * 2
        area_h = min(self.H, area_h)
        self._layout_cached = (area_h, (self.H - area_h) // 2)
        return self._layout_cached

    def _out_fps(self, info: ffmpeg.MediaInfo) -> tuple[float, str]:
        """Частота кадров эдита. По умолчанию — как у фильма: 24 к/с фильма в 30 к/с ролика дают повтор
        каждого 5-го кадра, и всё движение (наезды, проезды) равномерно подёргивается."""
        want = str(self.e.get("edit_fps", "source"))
        fps = float(info.fps) if want == "source" else float(want)
        for nominal, exact, text in ((23.976, 24000 / 1001, "24000/1001"), (29.97, 30000 / 1001, "30000/1001"),
                                     (59.94, 60000 / 1001, "60000/1001")):
            if abs(fps - nominal) < 0.01:
                return exact, text
        return fps, f"{fps:g}"

    def _fragment(self, movie: Path, info: ffmpeg.MediaInfo, src_start: float, n: int, speed_fx: str,
                  voice: bool, fx: dict, out: Path, layout: tuple[int, int] | None = None, grade: str = "",
                  film_volume: float = 0.3, norm: bool = False) -> float:
        """Рендерит один фрагмент ровно из n кадров: кадры по кривой скорости (со смешиванием кадров на
        замедлении), кадрирование по лицу, нейросетевое увеличение, эффекты, цветокоррекция.
        layout=(высота окна, отступ сверху) — видео по центру с чёрными полями (иначе на весь экран).
        Возвращает длину использованного куска фильма."""
        fps, fps_text = self._out_fps(info)
        movie = Path(movie).resolve()
        n = max(1, int(n))
        out_dur = n / fps
        area_h, top = layout if layout else (self.H, 0)
        speed = self._speed_curve(speed_fx, n, fps)
        ts = np.concatenate([[0.0], np.cumsum(speed[:-1])]) / fps          # время в куске фильма для кадра
        span = float(ts[-1]) + 0.3
        src_start = max(0.0, min(src_start, info.duration - span - 0.3))
        frames = self._decode(movie, info, src_start, span)
        if self._wm is not None:     # логотип канала в углу — закрашиваем
            frames = watermark.remove(frames, self._wm)
        try:                          # задвоенные кадры рипа (движение «спотыкается») — дорисовываем по движению
            frames, n_fix = interp.fix_duplicates(frames)
            if n_fix:
                log.info("    исправлено задвоенных кадров: %d", n_fix)
        except Exception as ex:
            log.warning("Исправление задвоенных кадров не удалось (%s)", ex)
        src_h = info.height
        try:                          # вшитые субтитры фильма внизу — срезаем полосу с ними (кадр чуть ближе)
            top = burned.detect(frames, info.fps)
            if top is not None:
                src_h = max(int(info.height * 0.72), int(info.height * (top - 0.015))) // 2 * 2
                frames = np.ascontiguousarray(frames[:, :src_h])
                log.info("    вшитые субтитры фильма с %.0f%% высоты — нижняя полоса срезана", top * 100)
        except Exception as ex:
            log.warning("Проверка вшитых субтитров не удалась (%s)", ex)
        # тёмный кусок (ночь, интерьер) — высветляем, чтобы на телефоне было видно лица
        luma = float(frames[:: max(1, len(frames) // 8), ::4, ::4].mean())
        target = float(self.e.get("min_luma", 72))
        if grade and luma < target:
            g = min(1.55, (target / max(luma, 8.0)) ** 0.55)
            grade = f"eq=gamma={g:.2f}," + grade
        try:
            framer = reframe.Framer(info.width, src_h, self.W, area_h, self._upscaler())
        except Exception as ex:  # видеокарта/нейросеть недоступна — обычное увеличение, но ролик будет
            log.warning("Нейросетевое увеличение недоступно (%s) — lanczos", ex)
            framer = reframe.Framer(info.width, src_h, self.W, area_h, None)
        mode = str(self.e.get("reframe", "track"))
        cut_frames = np.zeros(len(frames), bool)
        try:
            centers, cut_frames = reframe.analyze_frames(frames, info.fps)
            path = reframe.plan_path(centers, cut_frames, info.fps, framer.half, mode)
        except Exception as ex:  # кадрирование не должно ронять монтаж
            log.warning("Кадрирование: анализ не удался (%s), беру центр", ex)
            path = np.full(len(frames), 0.5)
        pos = np.clip(ts * info.fps, 0, len(frames) - 1)
        i0 = np.floor(pos).astype(int)
        i1 = np.minimum(i0 + 1, len(frames) - 1)
        w = pos - i0
        # время с последней родной склейки (для «удара» на склейках внутри сцены)
        last_cut, since = -1e9, np.zeros(n)
        for k in range(n):
            if cut_frames[i0[k]] and (k == 0 or i0[k] != i0[k - 1]):
                last_cut = k / fps
            since[k] = k / fps - last_cut

        # конец звука: у реплики — сразу после её последнего слова (хвост кадра до доли бита — без начала
        # следующей фразы), у остального — в конце фрагмента
        audio_end = fx.get("audio_end")
        if audio_end and audio_end < out_dur - 0.05:
            fade_at, fade_d = max(0.0, audio_end - 0.05), 0.15
        else:
            fade_at, fade_d = max(0.0, out_dur - 0.06), 0.06
        if voice or (speed_fx == "" and info.has_audio):
            vol = "" if voice else f"volume={film_volume},"   # экшен: звук фильма тихо под музыкой
            a_in = ["-ss", f"{src_start:.3f}", "-t", f"{out_dur + 0.2:.3f}", "-i", str(movie)]
            loud = "loudnorm=I=-16:TP=-2:LRA=11,aresample=48000," if norm else ""
            af = (f"[1:a:{info.audio_index}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                  f"{loud}{vol}afade=t=in:d=0.03,afade=t=out:st={fade_at:.3f}:d={fade_d:.3f},apad[a]")
        else:
            a_in = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            af = "[1:a]aformat=channel_layouts=stereo[a]"
        vf = "[0:v]" + (f"{grade}," if grade else "")
        if area_h < self.H:   # видео по центру, сверху и снизу — чёрные поля
            vf += f"pad={self.W}:{self.H}:0:{top}:black,"
        vf += "setsar=1,format=yuv420p[v]"
        cmd = [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.W}x{area_h}", "-framerate", fps_text, "-i", "-",
               *a_in, "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
               # ровно n кадров и столько же звука; звук без сжатия (pcm): у AAC в начале каждого куска есть
               # «разгон» ~20 мс — на склейках из-за него набегали микропаузы
               "-frames:v", str(n), "-t", f"{out_dur:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
               "-c:a", "pcm_s16le", "-ar", "48000", out.name]
        err = out.with_suffix(".log")
        with open(err, "wb") as errf:
            proc = subprocess.Popen(cmd, cwd=out.parent, stdin=subprocess.PIPE, stderr=errf)
            assert proc.stdin
            try:
                for b0 in range(0, n, 16):
                    sl = slice(b0, min(n, b0 + 16))
                    src = frames[i0[sl]].copy()
                    # замедление без «рывков» и без двоения: промежуточный кадр дорисовывается по движению
                    blend = (speed[sl] < 0.9) & (w[sl] > 0.12) & (w[sl] < 0.88)
                    for k in np.nonzero(blend)[0]:
                        g = b0 + k
                        if i1[g] != i0[g]:
                            src[k] = interp.between(frames[i0[g]], frames[i1[g]], float(w[g]))
                    # окно кадрирования — там же, где картинка: у дорисованного кадра (замедление) окно тоже
                    # на полпути между кадрами, иначе при слежении за лицом кадр «отстаёт» и картинка дрожит
                    ww = np.where(blend & (i1[sl] != i0[sl]) & ~np.asarray(cut_frames, bool)[i1[sl]], w[sl], 0.0)
                    big = framer(src, path[i0[sl]] * (1 - ww) + path[i1[sl]] * ww)
                    for k in range(len(big)):
                        big[k] = self._effects(big[k], (b0 + k) / fps, out_dur, fx, since[b0 + k])
                    proc.stdin.write(big.tobytes())
            except BrokenPipeError:
                pass
            proc.stdin.close()
            code = proc.wait()
        if code != 0:
            raise ffmpeg.FFmpegError(f"ffmpeg завершился с кодом {code}:\n"
                                     f"{err.read_text(encoding='utf-8', errors='replace')[-3000:]}")
        return float(ts[-1])

    # ---------------------------------------------------------------- эдит-сцена (формат эталона)
    def _build_scene(self, movie: Movie, an: Analysis, plan: EditPlan, work: Path,
                     track: Track | None) -> tuple[float, bool]:
        """Одна цельная сцена (родные склейки, реплики с субтитрами) + быстрая нарезка лучших кадров
        этой же сцены под бит в конце. Видео по центру (edit.zoom_pct), сверху — плашка с заголовком."""
        look = plan.look
        info = an.info
        fps, _ = self._out_fps(info)
        layout = self._layout(info)
        u = self._beat_unit(track)
        s, e = plan.segments[0]
        subs_on = bool(self.cfg.subtitles.enabled)
        words = subtitles.transcribe(self.cfg, movie.path, max(0.0, s - 1.0), e - s + 2, work,
                                     info.audio_index) if subs_on else []
        s, e = subtitles.snap_window(words, s, e, e - s + 2, slack=0.6)
        # основная часть: конец — на ближайшей доле музыки после последней реплики
        body_frames = int(round(np.ceil((e - s) / u - 0.15) * u * fps))
        out = work / "frag_00.mkv"
        log.info("  сцена: %s–%s (%.1f с)", ffmpeg.fmt_time(s), ffmpeg.fmt_time(s + body_frames / fps),
                 body_frames / fps)
        self._fragment(movie.path, info, s, body_frames, "", True,
                       {"punch": 0.0, "push": 0.04, "cut_punch": 0.05}, out, layout, look.grade)
        names = [f"file '{out.name}'"]
        body = body_frames / fps
        # финал: нарезка кадров этой же сцены под бит, с переходами
        beats_per = 1 if u >= 0.4 else 2
        n_fin = int(self.e.get("finale_shots", 12))
        shots = list(plan.finale)[:n_fin]
        cum, f_prev = 0, body_frames
        for i, t0 in enumerate(shots):
            cum += beats_per
            f_next = body_frames + int(round(cum * u * fps))
            n_frames, f_prev = f_next - f_prev, f_next
            tr = look.transitions[i % len(look.transitions)]
            strong = i % 4 == 0
            fxd = {"punch": 0.10, "push": 0.06, "transition": tr, "dir": 1 if i % 2 else -1,
                   "shake": look.shake if strong else 0, "flash": strong and i > 0}
            last = i == len(shots) - 1
            out = work / f"frag_{i + 1:02d}.mkv"
            self._fragment(movie.path, info, max(0.0, t0 - 0.1), n_frames if not last else n_frames * 2,
                           "slowmo" if last else "", False, fxd, out, layout, look.grade, film_volume=0.25)
            names.append(f"file '{out.name}'")
            if last:
                f_prev += n_frames
        total = round(f_prev / fps, 3)
        (work / "list.txt").write_text("\n".join(names), encoding="utf-8")
        ffmpeg.run(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"], cwd=work)
        plan.body_len = body
        log.info("Эдит-сцена «%s»: сцена %.1f с + финал %d кадров = %.1f с (бит %.2f с, образ %s)",
                 plan.theme, body, len(shots), total, u, look.name)
        ass = False
        if subs_on and words:
            out_words = [subtitles.Word(w.start - s, w.end - s, w.text) for w in words
                         if s - 0.1 <= w.start and w.end <= s + body + 0.1]
            area_h, top = layout
            margin_v = self.H - (top + area_h) + int(area_h * 0.20)    # нижняя часть окна с видео
            n = subtitles.build_ass(self.cfg, out_words, [], 0.0, body, self.W, self.H, work / "subs.ass",
                                    margin_v=margin_v, palette=look.palette)
            ass = n > 0
        return total, ass

    # ---------------------------------------------------------------- After Effects
    _ae_media: Path | None = None
    _ae_data: dict | None = None

    def _ae_on(self) -> bool:
        if self.safe_mode or str(self.e.get("engine", "ffmpeg")) != "ae":
            return False
        from ..ae.bridge import find_ae
        return find_ae(self.e.get("ae_path")) is not None

    def _arc_ae_data(self, plan: EditPlan, look, clips: list[dict], flashes: list, words: list,
                     total: float, drop_t: float, fps: float, work: Path) -> dict:
        """Описание монтажа для After Effects: куски, вспышки, субтитры, плашка, музыка с громкостью по времени."""
        a = self._arc()
        s = self.cfg.subtitles
        data = {"name": plan.theme[:60], "w": self.W, "h": self.H, "fps": fps, "total": total,
                "clips": clips, "flashes": flashes, "subs": [], "plate": None, "music": None}
        if s.enabled and words:
            data["subs"] = subtitles.cine_events(self.cfg, words, drop_t, look.palette)
        data["sub_style"] = {
            "top_font": str(s.get("ae_top_font", "Montserrat-SemiBold")),
            "main_font": str(s.get("ae_main_font", "Montserrat-ExtraBoldItalic")),
            "top_size": int(self.W * float(s.get("cine_top_pct", 3.9)) / 100),
            "main_size": int(self.W * float(s.get("cine_main_pct", 7.0)) / 100),
            "y_pct": float(s.get("cine_y_pct", 56)), "top_tracking": 120, "main_tracking": 20,
            "glow_threshold": 90, "glow_radius": 28, "glow_intensity": 1.3,
        }
        if plan.hook_text:
            data["plate"] = {"lines": _wrap(plan.hook_text, int(a.get("title_wrap", 34)))[:2],
                             "size": int(self.W * float(a.get("title_size_pct", 4.2)) / 100),
                             "dur": float(a.get("title_seconds", 9)), "font": str(a.get("plate_font", "Montserrat-Bold"))}
        if self._music:
            # музыка: кусок обработанного трека (дроп — на начале финала), громкость выровнена;
            # дальше уровни по времени: тише под репликами, «вдох» перед дропом, на дропе — в полную силу
            mus = self._ae_media / "music.wav"
            ffmpeg.run(["-ss", f"{self._music[1]:.3f}", "-t", f"{total + 0.5:.3f}", "-i", str(work / self._music[0]),
                        "-af", "loudnorm=I=-15:TP=-2:LRA=7,aresample=48000", "-ac", "2", "-c:a", "pcm_s16le", str(mus)])
            m = self.cfg.music
            low = float(m.get("bed_db_arc", -8))
            duck = low - float(m.get("duck_db_arc", 7))
            keys = {0.0: -40.0, 0.6: low}
            for c in clips:
                if c["voice"] and c["start"] < drop_t:
                    keys[round(c["start"] + 0.05, 3)] = low
                    keys[round(c["start"] + 0.25, 3)] = duck
                    keys[round(c["start"] + c["dur"] - 0.2, 3)] = duck
                    keys[round(c["start"] + c["dur"] + 0.05, 3)] = low
            keys = {t: v for t, v in keys.items() if t < drop_t - 0.4}
            keys[round(drop_t - 0.36, 3)] = low
            keys[round(drop_t - 0.30, 3)] = -26.0
            keys[round(drop_t - 0.02, 3)] = -26.0
            keys[round(drop_t, 3)] = 0.0
            keys[round(total - 1.5, 3)] = 0.0
            keys[round(total, 3)] = -50.0
            data["music"] = {"file": mus.resolve().as_posix(), "levels": sorted([t, v] for t, v in keys.items())}
        return data

    def _finalize_ae(self, out_file: Path, plan: EditPlan) -> None:
        from ..ae.bridge import AfterEffects, find_ae

        subtitles.unload_models()          # Whisper больше не нужен — освобождаем ~1.5 ГБ до рендера
        upscale.get.cache_clear()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        ae = AfterEffects(find_ae(self.e.get("ae_path")), Path(str(self.e.get("ae_bridge_dir", "C:/aebridge"))),
                          int(self.e.get("ae_mem_cache_pct", 20)), int(self.e.get("ae_mem_max_pct", 35)))
        render_to = self._ae_media / "render.mp4"
        data = dict(self._ae_data, render_to=render_to.resolve().as_posix())
        aep = ae.build(data, out_file.parent / "ae" / f"{out_file.stem}.aep")
        ae.render(aep, render_to)
        ffmpeg.run(["-i", str(render_to), "-c", "copy", "-movflags", "+faststart", str(out_file)])
        try:                                   # сверка субтитров с голосом по готовому ролику
            from .. import qa
            rep = qa.sync_report(self.cfg, out_file, data.get("subs") or [])
            if rep and rep["late_or_early_over_300ms"] and qa.realign(data["subs"], rep["heard"]):
                # слова, разошедшиеся с голосом, ставим туда, где они прозвучали, и рендерим ещё раз (~1 мин)
                log.info("Подвинул разошедшиеся с голосом слова субтитров — рендерю заново")
                subtitles.unload_models()
                aep = ae.build(data, out_file.parent / "ae" / f"{out_file.stem}.aep")
                render_to.unlink(missing_ok=True)
                ae.render(aep, render_to)
                out_file.unlink(missing_ok=True)
                ffmpeg.run(["-i", str(render_to), "-c", "copy", "-movflags", "+faststart", str(out_file)])
                qa.sync_report(self.cfg, out_file, data["subs"])
            qa.jitter_report(out_file, float(getattr(plan, "body_len", 0.0) or data["total"]))
        except Exception as ex:
            log.warning("Проверка синхронизации субтитров не удалась (%s)", ex)
        subtitles.unload_models()

    # ---------------------------------------------------------------- эдит-линия (формат эталонных каналов)
    _music = None

    @staticmethod
    def _speech_segments(words: list[subtitles.Word], a: float, b: float, max_pause: float,
                         u: float) -> list[tuple[float, int, list, float]]:
        """Реплика без пустых мест: куски речи, между которыми пауза дольше max_pause, идут встык (паузу
        вырезаем, склейка — джамп-кат). Кусок: (начало, долей, слова, длина речи). Длина куска — целое число
        долей, поэтому в конце куска остаётся не больше доли тишины."""
        if max_pause <= 0 or len(words) < 2:
            return [(a, max(2, int(np.ceil((b - a) / u - 0.05))), words, b - a)]
        groups, cur = [], [words[0]]
        for w in words[1:]:
            if w.start - cur[-1].end > max_pause:
                groups.append(cur)
                cur = []
            cur.append(w)
        groups.append(cur)
        segs = []
        for i, g in enumerate(groups):
            s0 = a if i == 0 else max(a, g[0].start - 0.12)
            s1 = b if i == len(groups) - 1 else g[-1].end + 0.2
            segs.append((s0, max(1, int(np.ceil((s1 - s0) / u - 0.05))), g, s1 - s0))
        return segs

    @staticmethod
    def _voice_tail(movie: Path, info: ffmpeg.MediaInfo, end: float, limit: float) -> float:
        """Страховка «по звуку»: Whisper иногда пропускает последние слова («…и проваливай»). Детектор речи
        (Silero VAD) слушает кусок после конца реплики: если голос продолжается без паузы больше 0.9 с —
        продлеваем до конца звучания, но не дальше limit (следующая фраза / +2.5 с)."""
        if limit <= end + 0.2:
            return end
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        sr = 16000
        t0 = max(0.0, end - 1.0)
        proc = ffmpeg.popen_raw(["-ss", f"{t0:.3f}", "-t", f"{limit - t0 + 0.3:.3f}", "-i", str(movie),
                                 "-map", f"0:a:{info.audio_index}", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
        assert proc.stdout
        y = np.frombuffer(proc.stdout.read(), np.float32).copy()
        proc.wait()
        if len(y) < sr // 2:
            return end
        opts = VadOptions(threshold=0.5, min_speech_duration_ms=150, min_silence_duration_ms=300, speech_pad_ms=60)
        segs = [(x["start"] / sr + t0, x["end"] / sr + t0) for x in get_speech_timestamps(y, opts, sampling_rate=sr)]
        new_end = end
        for a, b in sorted(segs):
            if b <= new_end:
                continue
            if a - new_end > 0.9:                       # пауза — дальше уже другая фраза
                break
            new_end = b
        return float(min(limit, max(end, new_end + 0.15 if new_end > end else end)))

    @staticmethod
    def _full_line(words: list[subtitles.Word], a: float, b: float, gap: float = 0.8,
                   max_len: float = 13.0) -> tuple[float, float, list[subtitles.Word]]:
        """Реплика целиком: от начала фразы до её конца. Слова внутри [a, b] — ядро; расширяем назад и вперёд,
        пока речь идёт без паузы (паузы короче gap — та же фраза или ответ собеседника); если реплика —
        вопрос и сразу звучит короткий ответ, берём и его. Границы — посередине тишины вокруг (не по слову)."""
        if not words:
            return a, b, []
        core = [i for i, w in enumerate(words) if w.end > a + 0.05 and w.start < b - 0.05]
        if not core:
            return a - 0.2, b + 0.4, []
        from ..arcs import continues

        i, j = core[0], core[-1]
        while i > 0 and words[j].end - words[i - 1].start <= max_len and (
                words[i].start - words[i - 1].end < gap
                or (words[i].start - words[i - 1].end < 2.0 and continues(words[i - 1].text, words[i].text))
                or (words[i].start - words[i - 1].end < 2.5 and words[i - 1].text.endswith("?"))):
            i -= 1                                       # реплика начинается с ответа — берём и вопрос
        while j + 1 < len(words) and words[j + 1].end - words[i].start <= max_len and (
                words[j + 1].start - words[j].end < gap
                or (words[j + 1].start - words[j].end < 2.0 and continues(words[j].text, words[j + 1].text))):
            j += 1
        while words[j].text.endswith("?") and j + 1 < len(words) and words[j + 1].start - words[j].end < 2.5:
            k = j + 1                                   # ответ на вопрос — часть смысла
            while k + 1 < len(words) and (words[k + 1].start - words[k].end < gap or (
                    words[k + 1].start - words[k].end < 2.0 and continues(words[k].text, words[k + 1].text))):
                k += 1
            if words[k].end - words[j + 1].start > 5.0 or words[k].end - words[i].start > max_len + 3:
                break
            j = k
        before = words[i].start - words[i - 1].end if i > 0 else 1.0
        after = words[j + 1].start - words[j].end if j + 1 < len(words) else 1.0
        start = words[i].start - min(0.3, before / 2)
        end = words[j].end + min(0.7, after * 0.6)
        return start, end, words[i:j + 1]

    def _cold_open(self, movie: Path, info: ffmpeg.MediaInfo, lines: list,
                   u: float) -> tuple[float, float, list] | None:
        """Фраза для холодного старта: законченное предложение 1.2–4 с из середины или конца сцены (конфликт,
        кульминация), в светлом кадре — человек в ленте сразу видит, к чему идёт сцена, и остаётся досмотреть.
        Возвращает (начало, длина, слова) или None."""
        cfg_a = self._arc()
        lo, hi = (float(x) for x in (cfg_a.get("cold_open_sec") or [1.2, 4.0]))
        min_luma = float(cfg_a.get("cold_open_luma", 55))
        scene0 = lines[0][0]
        scene_len = max(1.0, lines[-1][0] + lines[-1][3] - scene0)
        cands = []
        for a, _nb, words, _len, score, _segs in lines:
            sent: list = []
            for w in words:
                sent.append(w)
                if not w.text.rstrip().endswith((".", "!", "?", "…")) and w is not words[-1]:
                    continue
                s, e = sent[0].start - 0.1, sent[-1].end + 0.15
                if lo <= e - s <= hi and len(sent) >= 2 and s - scene0 > 3.0:
                    end = sent[-1].text.rstrip()
                    pts = score + (1.5 if end.endswith("!") else 1.0 if end.endswith("?") else 0.0) \
                        + 1.0 * (s - scene0) / scene_len + 0.3 * min(len(sent), 6)
                    cands.append((pts, s, e - s, list(sent)))
                sent = []
        for pts, s, ln, ws in sorted(cands, key=lambda c: -c[0])[:6]:
            try:
                luma = float(self._decode(movie, info, max(0.0, s), 0.6)[:, ::8, ::8].mean())
            except Exception:
                continue
            if luma >= min_luma:
                log.info("  холодный старт %s (%.1f с, яркость %.0f): «%s»", ffmpeg.fmt_time(s), ln, luma,
                         " ".join(w.text for w in ws))
                return s, ln, ws
            log.info("    фраза «%s» — тёмный кадр (%.0f), не для начала", " ".join(w.text for w in ws), luma)
        log.warning("  для холодного старта не нашлось светлой сильной фразы — ролик начнётся со сцены")
        return None

    def _arc(self) -> dict:
        return dict(self.cfg._data.get("arc") or {})

    def _build_arc(self, movie: Movie, an: Analysis, plan: EditPlan, work: Path,
                   track: Track | None) -> tuple[float, bool]:
        """Сюжетная линия: реплики героев по порядку (звучат из фильма, субтитры посередине кадра), между
        ними перебивки под музыку; к концу кадры короче (разгон); на дропе трека — вспышка и быстрая
        нарезка самых ярких кадров эпизода с переходами; последний кадр — замедление. Музыка замедлена
        с эхом («slowed + reverb») и сдвинута так, чтобы её дроп пришёлся ровно на начало финала."""
        cfg_a = self._arc()
        if track is None and self.cfg.music.source != "none":
            # без музыки у эдит-линии нет дропа и финал идёт в тишине — такой ролик не собираем
            raise RuntimeError("для эдит-линии не нашлось музыки")
        look = plan.look
        info = an.info
        fps, _ = self._out_fps(info)
        m = self.cfg.music
        mfx = None
        if track:
            try:
                mfx = musicfx.prepare(track.path, work, float(m.get("slowed", 0.88)), bool(m.get("reverb", True)))
            except Exception as ex:
                log.warning("Обработка музыки не удалась (%s) — беру трек как есть", ex)
        u = mfx.period if mfx else (track.beats.period if track else 0.6)
        while u < 0.42:
            u *= 2
        while u > 0.95:
            u /= 2
        story_max = float(cfg_a.get("story_sec", 32))
        finale_sec = float(cfg_a.get("finale_sec", 11))
        intro_beats = int(cfg_a.get("intro_beats", 4))           # 0 — ролик сразу начинается с реплики
        gap_lo, gap_hi = (int(x) for x in (cfg_a.get("gap_beats") or [2, 5]))
        n_predrop = int(cfg_a.get("predrop_shots", 3))
        loop = bool(cfg_a.get("loop", False))
        max_pause = float(cfg_a.get("max_pause_sec", 0.0))     # паузы длиннее внутри реплики вырезаем (0 — нет)

        # 1) реплики: точные границы по словам, длина — целое число долей
        lines = []
        for fr in [f for f in plan.fragments if f.voice]:
            ctx0 = max(0.0, fr.start - 4.0)
            words = subtitles.transcribe(self.cfg, movie.path, ctx0, fr.end - fr.start + 8, work, info.audio_index)
            a, b, words = self._full_line(words, fr.start, fr.end)
            nxt_word = next((w.start for w in subtitles.transcribe(self.cfg, movie.path, ctx0, fr.end - fr.start + 8,
                                                                   work, info.audio_index) if w.start > b), b + 3.0)
            b = self._voice_tail(movie.path, info, b, min(b + 2.5, nxt_word - 0.15))
            segs = self._speech_segments(words, a, b, max_pause, u)
            n_beats = sum(nb for _, nb, _, _ in segs)
            lines.append([a, n_beats, words, b - a, fr.score, segs])
            log.info("  реплика %s–%s: «%s»%s", ffmpeg.fmt_time(a), ffmpeg.fmt_time(b), " ".join(w.text for w in words),
                     f" — {len(segs)} куска, паузы вырезаны" if len(segs) > 1 else "")
        if not lines:
            raise RuntimeError("в линии нет ни одной реплики")
        # реплики, расширенные до целых фраз, заходят друг на друга (сцена: реплики подряд) — склеиваем в одну,
        # иначе кусок фразы звучит дважды
        lines.sort(key=lambda x: x[0])
        merged = [lines[0]]
        for ln in lines[1:]:
            cur = merged[-1]
            cur_end = cur[0] + cur[3]
            if ln[0] < cur_end - 0.1:
                b = max(cur_end, ln[0] + ln[3])
                last = cur[2][-1].end if cur[2] else cur[0]       # слова первой кончаются раньше её границы
                words = cur[2] + [w for w in ln[2] if w.start >= last - 0.05]
                segs = self._speech_segments(words, cur[0], b, max_pause, u)
                merged[-1] = [cur[0], sum(nb for _, nb, _, _ in segs), words, b - cur[0], max(cur[4], ln[4]), segs]
                log.info("  реплики %s и %s заходят друг на друга — склеил", ffmpeg.fmt_time(cur[0]),
                         ffmpeg.fmt_time(ln[0]))
            else:
                merged.append(ln)
        lines = merged
        # не длиннее story_sec: убираем самые длинные реплики из середины (первую и последнюю оставляем)
        # бюджет истории: реплики целиком; если не влезают — сначала короче перебивки (до 1 короткой между
        # репликами), и только потом убираем реплику (наименее важную, кроме последней — развязки)
        fixed = lambda: sum(x[1] for x in lines) * u + (intro_beats + n_predrop) * u   # реплики + вступление + разгон
        min_keep = int(cfg_a.get("min_lines", 3))
        while len(lines) > min_keep and fixed() + (len(lines) - 1) * gap_lo * u > story_max:
            # не трогаем первую (крючок) и последнюю (развязку), если есть из чего выбирать
            if cfg_a.get("scene"):
                lines.pop()           # цельная сцена: середину не выкидываем (рвётся смысл) — укорачиваем с конца
                continue
            pool_k = range(1, len(lines) - 1) if len(lines) > 2 else range(0, len(lines) - 1)
            lines.pop(min(pool_k, key=lambda k: (lines[k][4], -lines[k][1])))
        gaps = max(1, len(lines) - 1)
        per_gap = int(np.clip((story_max - fixed()) / gaps / u, gap_lo, gap_hi))   # долей перебивок между репликами

        # 2) перебивки между репликами — кадры эпизода по порядку фильма
        span = (lines[0][0] - 12, lines[-1][0] + lines[-1][3] + 12)
        near = lambda ts, pad=0.0: [t for t in ts if span[0] - pad <= t <= span[1] + pad]
        all_broll = sorted(f.start for f in plan.fragments if not f.voice)
        pool = near(all_broll)
        if len(pool) < 10:
            pool = near(all_broll, 15)
        used: set[float] = set()
        text_cache: dict[float, bool] = {}

        def has_text(t: float) -> bool:
            """В кадре титры/вшитые субтитры фильма — для перебивок и финала такой кадр не берём."""
            if t not in text_cache:
                try:
                    fr = self._decode(movie.path, info, max(0.0, t), 1.5)
                    # провал в черноту (кто-то заслонил камеру, затемнение) — 5+ кадров почти чёрные
                    luma = fr[:, ::8, ::8].mean(axis=(1, 2, 3))
                    dark = np.convolve((luma < 24).astype(int), np.ones(4, int), "valid").max(initial=0) >= 4
                    if self._wm is not None:
                        fr = watermark.remove(fr, self._wm)
                    text_cache[t] = dark or burned.detect(fr, info.fps) is not None
                    why = "кадр уходит в черноту" if dark else "в кадре текст (титры)"
                except Exception:
                    text_cache[t] = False
                if text_cache[t]:
                    log.info("    кадр %s: %s — беру другой", ffmpeg.fmt_time(t), why)
            return text_cache[t]

        def take(after: float, before: float) -> float:
            while True:
                free = [t for t in pool if t not in used]
                if not free:
                    return after + 1.0
                between = [t for t in free if after < t < before]
                t = between[len(between) // 2] if between else min(free, key=lambda x: abs(x - after))
                used.add(t)
                if not has_text(t):
                    return t

        items = []   # (начало в фильме, долей, скорость, реплика?, эффекты, слова)
        hook = self._cold_open(movie.path, info, lines, u) if cfg_a.get("cold_open") else None
        if hook:
            # холодный старт: самая сильная фраза сцены в светлом кадре, вспышка — и сцена с начала
            h0, h_len, h_words = hook
            items.append((h0, max(2, int(np.ceil(h_len / u - 0.05))), "", True,
                          {"push": 0.05, "punch": 0.06, "audio_end": h_len}, h_words))
        if intro_beats:
            items.append((take(lines[0][0] - 60, lines[0][0]), intro_beats, "slowmo", False, {"push": 0.08}, []))
        for k, (a, nb, words, speech_len, _score, segs) in enumerate(lines):
            for s0, nb_s, ws, s_len in segs:
                fx_s = {"push": 0.05, "cut_punch": 0.03, "audio_end": s_len}
                if hook and len(items) == 1:
                    fx_s.update(flash=True, punch=0.08)
                items.append((s0, nb_s, "", True, fx_s, ws))
            end_line = a + speech_len
            if k + 1 < len(lines):
                nxt = lines[k + 1][0]
                # разгон: в начале одна длинная перебивка (замедленная), ближе к дропу — короче и чаще
                late = k >= len(lines) // 2
                parts = [per_gap] if not late or per_gap < 4 else [per_gap // 2, per_gap - per_gap // 2]
                if per_gap <= 0:
                    parts = []                                  # цельная сцена: реплики встык, без перебивок
                for j, nb_b in enumerate(parts):
                    items.append((take(end_line, nxt), nb_b, "" if late else "slowmo", False,
                                  {"push": 0.07, "punch": 0.05 if late else 0.0}, []))
            else:                                               # перед дропом — несколько коротких кадров по доле
                for j in range(n_predrop):
                    items.append((take(end_line, end_line + 60), 1, "", False, {"push": 0.06, "punch": 0.08}, []))

        # 3) финал после дропа
        # первые 2 кадра финала по 2 доли, дальше по одной — склейки ускоряются (как у эталонов)
        n_fin = max(4, int(round(finale_sec / u)) - (2 if loop else 0) - 2)   # последний кадр (3 доли) — в счёт финала
        fin_src = near(plan.finale) if len(near(plan.finale)) >= n_fin // 2 else near(plan.finale, 15)
        fin = []
        for t in [t for t in fin_src if t not in used] + [t for t in pool if t not in used and t not in fin_src]:
            if len(fin) >= n_fin:
                break
            if not has_text(t):
                fin.append(t)
        if len(fin) < 3:
            # ночная сцена: почти все кадры тёмные — без финала ролик выйдет без дропа и субтитров, не собираем
            raise RuntimeError(f"для финала нашлось только {len(fin)} светлых кадров — сцена не годится")
        trs = look.transitions or ["whip"]
        fin_items = []
        for i, t0 in enumerate(fin):
            last = i == len(fin) - 1
            fxd = {"punch": 0.20 if i == 0 else 0.11, "push": 0.06,
                   "transition": "" if i == 0 else trs[i % len(trs)], "dir": 1 if i % 2 else -1,
                   "shake": look.shake * 1.6 if i == 0 else (look.shake if i % 4 == 0 else 0),
                   "flash": i == 0 or i % 4 == 0}
            if last and loop:
                # петля: ролик кончается кадром, с которого начинается (за 1 с до первой реплики, замедленно) —
                # при повторе в ленте Shorts шва не видно, и ролик пересматривают
                t0, fxd = max(0.0, (hook[0] if hook else lines[0][0]) - 3 * u * 0.5), {
                    "push": 0.05, "transition": fxd["transition"],
                                                                 "dir": fxd["dir"]}
            fin_items.append((t0, 3 if last else (2 if i < 2 else 1), "slowmo" if last else "", False, fxd, []))

        # 4) рендер: склейка k — на кадре round(доля_k * u * fps)
        names, out_words = [], []
        cum, f_prev = 0, 0
        drop_t = 0.0
        all_items = items + fin_items
        ae = self._ae_media is not None
        clips, flashes = [], []
        for i, (src, nb, spd, voice, fxd, words) in enumerate(all_items):
            if i == len(items):
                drop_t = f_prev / fps
            cum += nb
            f_next = int(round(cum * u * fps))
            n_frames, f_prev = f_next - f_prev, f_next
            t_item = (f_next - n_frames) / fps
            out = (self._ae_media / f"frag_{i:02d}.mov") if ae else work / f"frag_{i:02d}.mkv"
            log.info("  %s %s: %s, %d доли%s", "финал" if i >= len(items) else "кадр", i + 1,
                     ffmpeg.fmt_time(src), nb, " (реплика)" if voice else "")
            # для After Effects движение, тряска, переходы и вспышки — живыми слоями там, а не «впечатаны» в кадр
            baked = {"audio_end": fxd.get("audio_end")} if ae else fxd
            self._fragment(movie.path, info, max(0.0, src), n_frames, spd, voice, baked, out, None,
                           look.cine or look.grade, film_volume=0.0, norm=ae and voice)
            names.append(f"file '{out.name}'")
            kind = "финал" if i >= len(items) else ("реплика" if voice else "перебивка")
            clips.append({"file": out.resolve().as_posix(), "start": round(t_item, 4), "dur": round(n_frames / fps, 4),
                          "name": f"{i + 1:02d} {kind} {ffmpeg.fmt_time(src)}", "voice": bool(voice),
                          "fx": {k: fxd[k] for k in ("push", "punch", "shake", "transition", "dir") if fxd.get(k)}})
            if fxd.get("flash"):
                flashes.append([round(t_item, 4), 90 if i in (len(items), 1) else 70])
            if voice:
                dur = n_frames / fps
                out_words += [subtitles.Word(w.start - src + t_item, w.end - src + t_item, w.text)
                              for w in words if src - 0.05 <= w.start and w.end <= src + dur + 0.05]
        total = round(f_prev / fps, 3)
        plan.body_len = drop_t
        if mfx:
            self._music = (mfx.path.name, musicfx.choose_offset(mfx, drop_t, total))
        if ae:
            self._ae_data = self._arc_ae_data(plan, look, clips, flashes, out_words, total, drop_t, fps, work)
            log.info("Эдит-линия «%s» для After Effects: %d реплик, дроп на %.1f с, всего %.1f с (доля %.2f с)",
                     plan.theme, len(lines), drop_t, total, u)
            return total, False
        (work / "list.txt").write_text("\n".join(names), encoding="utf-8")
        ffmpeg.run(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"], cwd=work)
        log.info("Эдит-линия «%s»: %d реплик, дроп на %.1f с, финал %d кадров, всего %.1f с (доля %.2f с, образ %s)",
                 plan.theme, len(lines), drop_t, len(fin_items), total, u, look.name)
        ass = False
        if self.cfg.subtitles.enabled and out_words:
            n = subtitles.build_cine_ass(self.cfg, out_words, drop_t, self.W, self.H, work / "subs.ass",
                                         palette=look.palette)
            ass = n > 0
        return total, ass

    def _build_edit(self, movie: Movie, an: Analysis, plan: EditPlan, work: Path,
                    track: Track | None) -> tuple[float, bool]:
        """Эдит: фрагменты по 1–5 с, склейки ровно в долю трека. Реплики (voice) звучат из фильма,
        с субтитрами; между ними — экшен под музыку с эффектами."""
        u = self._beat_unit(track)
        max_voice = float(self.e.get("max_fragment", 5.0)) + 1.0     # реплику не обрываем, но и не растягиваем
        max_broll = float(self.e.get("max_broll", 2.5))               # экшен-кадр: 1–2.5 с — динамика
        max_total = float(self.e.get("max_total", 30.0))
        subs_on = bool(self.cfg.subtitles.enabled)
        # 1) длительности: каждая склейка — ровно в долю трека
        items = []   # [фрагмент, начало в фильме, длительность в долях, слова]
        for fr in plan.fragments:
            s, e = fr.start, fr.end
            words: list[subtitles.Word] = []
            if fr.voice:
                if subs_on:
                    words = subtitles.transcribe(self.cfg, movie.path, max(0.0, s - 1.0), e - s + 2, work,
                                                 an.info.audio_index)
                    # границы куратора уже стоят на паузах — только чуть подправляем, чтобы не резать слово
                    s, e = subtitles.snap_window(words, s, e, max_voice, slack=0.4)
                beats = max(1, int(np.ceil((e - s + 0.1) / u - 0.1)))   # конец — ближайшая доля после фразы
            else:
                rate = {"slowmo": 0.5, "ramp": 0.6, "fast": 1.5}.get(fr.fx, 1.0)
                want = min((e - s) / rate, max_broll)
                beats = max(1, int(round(want / u)))
            items.append([fr, s, beats, words])
        # 2) не длиннее max_total: сначала укорачиваем экшен-кадры (до 2 долей), потом убираем лишние из середины
        total_beats = lambda: sum(it[2] for it in items)
        while total_beats() * u > max_total:
            broll = [it for it in items if not it[0].voice and it[2] > 2]
            if broll:
                max(broll, key=lambda it: it[2])[2] -= 1
                continue
            mid = [k for k, it in enumerate(items) if not it[0].voice and 0 < k < len(items) - 1]
            if not mid:
                break
            items.pop(mid[len(mid) // 2])
        # 3) рендер: склейка k — на кадре round(доля_k * u * fps) общей шкалы (без накопления погрешности)
        fps, _ = self._out_fps(an.info)
        t, cum, f_prev = 0.0, 0, 0
        names, out_words = [], []
        for i, (fr, s, beats, words) in enumerate(items):
            cum += beats
            f_next = int(round(cum * u * fps))
            n_frames, f_prev = f_next - f_prev, f_next
            out_dur = n_frames / fps
            strong = i > 0 and not fr.voice and (i % 2 == 0 or fr.fx in ("ramp", "slowmo"))
            fx = {"punch": 0.05 if fr.voice else 0.12, "push": 0.03 if fr.voice else 0.05,
                  "shake": 0 if fr.voice else (16 if strong else 0), "flash": bool(strong)}
            out = work / f"frag_{i:02d}.mkv"
            log.info("  фрагмент %d: %s +%.1f с%s%s", i + 1, ffmpeg.fmt_time(s), out_dur,
                     " реплика" if fr.voice else "", f" {fr.fx}" if fr.fx else "")
            self._fragment(movie.path, an.info, s, n_frames, "" if fr.voice else fr.fx, fr.voice, fx, out)
            names.append(f"file '{out.name}'")
            out_words += [subtitles.Word(w.start - s + t, w.end - s + t, w.text)
                          for w in words if s - 0.1 <= w.start and w.end <= s + out_dur + 0.1]
            t += out_dur
        (work / "list.txt").write_text("\n".join(names), encoding="utf-8")
        ffmpeg.run(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mkv"], cwd=work)
        total = round(t, 3)
        log.info("Эдит «%s»: %d фрагментов, %.1f с, склейки каждые %.2f с — %s",
                 plan.theme, len(names), total, u, plan.thought)
        ass = False
        if subs_on and out_words:
            n = subtitles.build_ass(self.cfg, out_words, [], 0.0, total, self.W, self.H, work / "subs.ass")
            ass = n > 0
        return total, ass

    # ---------------------------------------------------------------- сведение
    def _audio_graph(self, track: Track | None, total: float, kind: str, body: float = 0.0) -> str:
        m = self.cfg.music
        film_lufs = float(m.film_lufs)
        fade_out = f"afade=t=out:st={max(0.0, total - 1.2):.2f}:d=1.2"
        film = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"loudnorm=I={film_lufs}:TP=-2:LRA=11,aresample=48000")
        if not track:
            return f"{film},{fade_out}[a]"
        if kind in ("scene", "arc"):
            # сцена: музыка тихой подложкой (и ещё тише под репликами); финал — музыка на первом плане
            low = 10 ** (float(m.get("bed_db_arc", -8) if kind == "arc" else m.get("bed_db_scene", -13)) / 20)
            if kind == "arc":
                # перед дропом музыка на 0.3 с почти пропадает («вдох»), на дропе — сразу в полную силу
                vol = (f"if(lt(t,{body - 0.32:.3f}),{low:.4f},if(lt(t,{body:.3f}),{low * 0.12:.4f},1))")
                return (
                    f"{film},asplit=2[f][key];"
                    f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                    f"apad,loudnorm=I={film_lufs + 1}:TP=-2:LRA=7,aresample=48000,"
                    f"volume=eval=frame:volume='{vol}',"
                    f"afade=t=in:d=0.6,afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5[m];"
                    f"[m][key]sidechaincompress=threshold=0.03:ratio={float(m.duck_ratio)}:attack=20:release=350[md];"
                    f"[f][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95,{fade_out}[a]"
                )
            return (
                f"{film},asplit=2[f][key];"
                f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                f"loudnorm=I={film_lufs}:TP=-3:LRA=7,aresample=48000,"
                f"volume=eval=frame:volume='if(lt(t,{body - 0.25:.3f}),{low:.4f},"
                f"if(lt(t,{body + 0.05:.3f}),{low:.4f}+(1-{low:.4f})*(t-{body - 0.25:.3f})/0.3,1))',"
                f"afade=t=in:d=0.8,afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5[m];"
                f"[m][key]sidechaincompress=threshold=0.03:ratio={float(m.duck_ratio)}:attack=30:release=400[md];"
                f"[f][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9,{fade_out}[a]"
            )
        # музыка на bed_db тише фильма и дополнительно приглушается, когда в фильме звучит речь/эффекты
        bed = float(m.bed_db_scene if kind == "story" else m.get("bed_db_edit", -4) if kind == "edit"
                    else m.bed_db_montage)
        return (
            f"{film},asplit=2[f][key];"
            f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
            f"loudnorm=I={film_lufs + bed}:TP=-3:LRA=7,aresample=48000,"
            f"afade=t=in:d=1.0,afade=t=out:st={max(0.0, total - 2.0):.2f}:d=2.0[m];"
            f"[m][key]sidechaincompress=threshold=0.03:ratio={float(m.duck_ratio)}:attack=30:release=500[md];"
            f"[f][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9,{fade_out}[a]"
        )

    def _finalize(self, work: Path, track: Track | None, total: float, plan: EditPlan, ass: bool,
                  final: Path, kind: str) -> None:
        e = self.e
        args = ["-i", "joined.mkv" if (work / "joined.mkv").exists() else "joined.mp4"]
        if track and self._music:      # обработанный трек (slowed + reverb) — с места, где дроп попадает в финал
            args += ["-ss", f"{self._music[1]:.3f}", "-i", self._music[0]]
        elif track:
            shutil.copy(track.path, work / f"music{track.path.suffix.lower()}")
            args += ["-stream_loop", "-1", "-ss", f"{track.beats.offset:.3f}",
                     "-i", f"music{track.path.suffix.lower()}"]

        vf = ["fade=t=in:st=0:d=0.3"]
        has_font = self._prepare_font(work)
        # обработка картинки эдита: контраст, тил-оранж, мягкое свечение, резкость, виньетка, лёгкое зерно
        grade = ""
        if kind in ("edit", "montage") and e.grade:
            grade = ("eq=contrast=1.10:saturation=1.14:gamma=0.97,"
                     "colorbalance=rs=-0.04:gs=-0.01:bs=0.05:rh=0.05:gh=0.01:bh=-0.05,"
                     # свечение только по яркости и в yuv444p: иначе после colorbalance формат кадра другой,
                     # смешиваются цветовые каналы — картинка лиловеет и у правого края появляется полоса
                     "format=yuv444p,split[g0][g1];[g1]gblur=sigma=28[g2];"
                     "[g0][g2]blend=c0_mode=screen:c0_opacity=0.16:c1_opacity=0:c2_opacity=0,format=yuv420p,"
                     "unsharp=5:5:0.45,vignette=PI/6,noise=alls=2:allf=t,")
        if ass:
            vf.append("ass=subs.ass:fontsdir=fonts" if has_font else "ass=subs.ass")
        if kind == "arc" and plan.hook_text and has_font:
            # тонкая белая полоса у самого верха с чёрной фразой (как у эталонных эдитов), первые секунды
            a = self._arc()
            size = int(self.W * float(a.get("title_size_pct", 4.2)) / 100)
            lines = _wrap(plan.hook_text, int(a.get("title_wrap", 34)))[:2]
            box_h = int(size * 1.3 * len(lines) + size * 0.75)
            dur = float(a.get("title_seconds", 9))
            vf.append(f"drawbox=x=0:y=0:w={self.W}:h={box_h}:color=white@1.0:t=fill:enable='between(t,0,{dur})'")
            tfont = "fonts/plate.ttf" if (work / "fonts" / "plate.ttf").exists() else "fonts/font.ttf"
            for i, line in enumerate(lines):
                (work / f"title{i}.txt").write_text(line, encoding="utf-8")
                vf.append(f"drawtext=fontfile={tfont}:textfile=title{i}.txt:fontsize={size}:fontcolor=black"
                          f":x=(w-text_w)/2:y={int(size * 0.38)}+{i}*{int(size * 1.3)}"
                          f":enable='between(t,0,{dur})'")
        elif kind == "scene" and plan.hook_text and has_font:
            # белая плашка над видео с заголовком (как в эталоне): чёрный плотный текст капсом, первые секунды
            area_h, top = self._layout_cached
            size = int(self.W * float(e.get("title_size_pct", 5.0)) / 100)
            lines = _wrap(plan.hook_text.upper(), 22)[:2]
            box_h = int(size * 1.35 * len(lines) + size * 0.9)
            y_box = max(0, top - box_h + 6)
            dur = float(e.get("title_seconds", 5.5))
            vf.append(f"drawbox=x=0:y={y_box}:w={self.W}:h={box_h}:color=white@0.97:t=fill"
                      f":enable='between(t,0,{dur})'")
            tfont = "fonts/title.ttf" if (work / "fonts" / "title.ttf").exists() else "fonts/font.ttf"
            for i, line in enumerate(lines):
                (work / f"title{i}.txt").write_text(line, encoding="utf-8")
                vf.append(f"drawtext=fontfile={tfont}:textfile=title{i}.txt:fontsize={size}:fontcolor=black"
                          f":x=(w-text_w)/2:y={y_box + int(size * 0.45)}+{i}*{int(size * 1.35)}"
                          f":enable='between(t,0,{dur})'")
        elif e.hook_text and plan.hook_text and has_font:
            # спокойная подпись-контекст, а не кричащий заголовок: обычный шрифт, небольшой размер,
            # мягкая тень вместо толстой обводки, плавно появляется и исчезает
            size = int(self.W * float(e.get("hook_size_pct", 4.0)) / 100)
            lines = _wrap(plan.hook_text, 30 if self.H > self.W else 48)[:2]
            y0 = int(self.H * 0.10)
            font = "fonts/hook.ttf" if (work / "fonts" / "hook.ttf").exists() else "fonts/font.ttf"
            for i, line in enumerate(lines):
                (work / f"hook{i}.txt").write_text(line, encoding="utf-8")
                vf.append(
                    f"drawtext=fontfile={font}:textfile=hook{i}.txt:fontsize={size}:fontcolor=white@0.92"
                    f":shadowcolor=black@0.55:shadowx=0:shadowy=2:borderw=1:bordercolor=black@0.25"
                    f":x=(w-text_w)/2:y={y0}+{i}*{int(size * 1.3)}"
                    f":alpha='if(lt(t,0.6),max(0,(t-0.3)/0.3),if(gt(t,3.0),max(0,(3.6-t)/0.6),1))'"
                    f":enable='between(t,0.3,3.6)'"
                )
        vf.append(f"fade=t=out:st={max(0.0, total - 0.6):.2f}:d=0.6")

        ffmpeg.run([
            *args, "-filter_complex",
            f"[0:v]{grade}{','.join(vf)}[v];{self._audio_graph(track, total, kind, plan.body_len)}",
            "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
            # потолок битрейта: YouTube всё равно пережимает, больше 16 Мбит/с смысла нет
            "-c:v", "libx264", "-preset", str(e.preset), "-crf", str(e.crf), "-profile:v", "high",
            "-maxrate", str(e.get("max_bitrate", "16M")), "-bufsize", "32M",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "final.mp4",
        ], cwd=work)
        shutil.move(str(work / "final.mp4"), final)

    def _prepare_font(self, work: Path) -> bool:
        candidates = [Path(str(self.e.font)), Path("C:/Windows/Fonts/arialbd.ttf"),
                      Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                      Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")]
        hook = Path(str(self.e.get("hook_font", "C:/Windows/Fonts/arial.ttf")))
        for font in candidates:
            if font.exists():
                (work / "fonts").mkdir(exist_ok=True)
                shutil.copy(font, work / "fonts" / "font.ttf")
                if hook.exists():
                    shutil.copy(hook, work / "fonts" / "hook.ttf")
                sub = Path(str(self.cfg.subtitles.get("font_file", "C:/Windows/Fonts/seguibl.ttf")))
                if sub.exists():
                    shutil.copy(sub, work / "fonts" / "sub.ttf")
                from ..config import ROOT
                pop = ROOT / str(self.cfg.subtitles.get("pop_font_file", "models/fonts/RussoOne-Regular.ttf"))
                if pop.exists():
                    shutil.copy(pop, work / "fonts" / "pop.ttf")
                for f in (ROOT / "models" / "fonts").glob("Montserrat-*.ttf"):
                    shutil.copy(f, work / "fonts" / f.name)
                plate = ROOT / "models" / "fonts" / "Montserrat-Bold.ttf"
                if plate.exists():
                    shutil.copy(plate, work / "fonts" / "plate.ttf")
                title = Path(str(self.e.get("title_font", "C:/Windows/Fonts/ariblk.ttf")))
                if title.exists():
                    shutil.copy(title, work / "fonts" / "title.ttf")
                return True
        log.warning("Шрифт не найден — текст и субтитры без своего шрифта (укажите edit.font в config.yaml)")
        return False
