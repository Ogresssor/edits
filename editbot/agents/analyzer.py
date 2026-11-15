"""Агент-аналитик: сигнальный анализ всего фильма, поиск цельных сцен и пиковых моментов.

Сцены (12–30 с, границы на склейках) — материал для основных эдитов «одна сцена — один ролик».
Пиковые моменты — материал для общего эдита-нарезки по фильму.

Сигналы (считаются с шагом 1/sample_fps секунды):
  * громкость звука (RMS, дБ) и её резкие всплески — взрывы, удары, крики, музыкальные кульминации;
  * «дроп» — резкий скачок громкости после тихого участка (саспенс -> удар);
  * движение в кадре — средняя разница соседних кадров;
  * темп монтажа — плотность склеек в окрестности (экшн-сцены режут часто);
  * насыщенность и яркость — чтобы отбрасывать чёрные экраны и тёмную «кашу».
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .. import ffmpeg
from ..config import Config
from .trends import Movie

log = logging.getLogger("analyzer")

FRAME_W, FRAME_H = 96, 54
AUDIO_SR = 8000
ANALYSIS_VERSION = 4  # повышать при изменении алгоритма — старый кэш анализа станет недействительным


@dataclass
class Moment:
    id: int
    peak: float          # самая сильная точка, сек
    start: float         # границы плана вокруг пика
    end: float
    score: float         # сводная интенсивность (z-оценка)
    loudness: float
    motion: float
    cut_rate: float
    drop: float
    keyframe: str = ""   # путь к jpg-кадру для куратора
    # заполняется куратором
    ai_score: float | None = None
    tags: list[str] | None = None
    slowmo: bool = False
    speech: str = ""     # реплики из субтитров в окрестности момента


@dataclass
class Scene:
    id: int
    start: float
    end: float
    peak: float
    score: float
    speech: str = ""                 # что говорят в сцене (распознанная речь + перевод из .srt)
    preview: str = ""                # картинка 2x2 из четырёх кадров сцены
    ai_score: float | None = None    # оценка куратора


@dataclass
class Analysis:
    moments: list[Moment]
    scenes: list[Scene]
    info: ffmpeg.MediaInfo
    workdir: Path
    subs: list[tuple[float, float, str]]
    cuts: list[float]          # время склеек (для подгонки границ кусков)
    story: Path                # карта сюжета (story.txt) — фильм как сценарий для куратора


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def load_subtitles(movie_path: Path) -> list[tuple[float, float, str]]:
    """Ищет .srt рядом с фильмом (то же имя или единственный .srt в папке)."""
    same = movie_path.with_suffix(".srt")
    srts = [same] if same.exists() else sorted(movie_path.parent.glob("*.srt"))
    if not srts:
        return []
    raw = srts[0].read_bytes()
    text = ""
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    subs = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lines = block[m.end():].strip().splitlines()
        line = re.sub(r"<[^>]+>|\{[^}]+\}", "", " ".join(lines)).strip()
        if line:
            subs.append((start, end, line))
    return subs


def movie_workdir(cfg: Config, movie_path: Path) -> Path:
    st = movie_path.stat()
    digest = hashlib.sha1(f"{movie_path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]
    d = cfg.paths.path("work") / digest
    d.mkdir(parents=True, exist_ok=True)
    return d


def _robust_z(x: np.ndarray) -> np.ndarray:
    med = np.median(x)
    iqr = np.subtract(*np.percentile(x, [75, 25])) or (np.std(x) or 1.0)
    return (x - med) / iqr


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) < n:
        return x
    return np.convolve(x, np.ones(n) / n, mode="same")


class AnalyzerAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.a = cfg.analysis

    # ---------- извлечение сигналов ----------
    def _video_signals(self, path: Path) -> dict[str, np.ndarray]:
        fps = float(self.a.sample_fps)
        args = []
        if str(self.a.hwaccel).lower() != "none":
            args += ["-hwaccel", str(self.a.hwaccel)]
        args += ["-i", str(path), "-map", "0:v:0", "-an", "-sn",
                 "-vf", f"fps={fps},scale={FRAME_W}:{FRAME_H}:flags=area",
                 "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        proc = ffmpeg.popen_raw(args)
        size = FRAME_W * FRAME_H * 3
        motion, cuts, bright, sat = [], [], [], []
        prev_gray = prev_hist = None
        assert proc.stdout
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            rgb = np.frombuffer(buf, np.uint8).reshape(FRAME_H, FRAME_W, 3).astype(np.float32) / 255.0
            gray = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
            hist = np.histogram(gray, bins=32, range=(0, 1))[0] / gray.size
            if prev_gray is None:
                motion.append(0.0)
                cuts.append(0.0)
            else:
                motion.append(float(np.mean(np.abs(gray - prev_gray))))
                cuts.append(float(np.abs(hist - prev_hist).sum()))
            bright.append(float(gray.mean()))
            sat.append(float((rgb.max(axis=2) - rgb.min(axis=2)).mean()))
            prev_gray, prev_hist = gray, hist
            if len(motion) % int(600 * fps) == 0:
                log.info("  видео проанализировано: %d мин", len(motion) // int(60 * fps))
        proc.wait()
        return {k: np.array(v, np.float32) for k, v in
                (("motion", motion), ("cut", cuts), ("bright", bright), ("sat", sat))}

    def _audio_rms(self, path: Path, n: int, has_audio: bool, audio_index: int = 0) -> np.ndarray:
        if not has_audio:
            return np.full(n, -60.0, np.float32)
        hop = int(AUDIO_SR / float(self.a.sample_fps))
        proc = ffmpeg.popen_raw(["-i", str(path), "-map", f"0:a:{audio_index}", "-vn", "-ac", "1",
                                 "-ar", str(AUDIO_SR), "-f", "s16le", "-"])
        rms = []
        assert proc.stdout
        while True:
            buf = proc.stdout.read(hop * 2)
            if len(buf) < hop * 2:
                break
            s = np.frombuffer(buf, np.int16).astype(np.float32) / 32768.0
            rms.append(20 * np.log10(np.sqrt(np.mean(s * s)) + 1e-5))
        proc.wait()
        out = np.full(n, -60.0, np.float32)
        out[:min(n, len(rms))] = rms[:n]
        return out

    def _params(self) -> dict:
        """Параметры, от которых зависит результат анализа: изменились в конфиге — анализ пересчитывается."""
        return {**self.a._data, "subtitles": bool(self.cfg.subtitles.enabled),
                "story_model": str(self.cfg.subtitles.story_model)}

    # ---------- основной метод ----------
    def analyze(self, movie: Movie) -> Analysis:
        workdir = movie_workdir(self.cfg, movie.path)
        info = ffmpeg.probe(movie.path)
        subs = load_subtitles(movie.path)
        cache = workdir / "analysis.json"
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("version") == ANALYSIS_VERSION and data.get("params") == self._params():
                moments = [Moment(**m) for m in data["moments"]]
                scenes = [Scene(**sc) for sc in data["scenes"]]
                if all(Path(m.keyframe).exists() for m in moments) and \
                        all(Path(sc.preview).exists() for sc in scenes) and (workdir / "story.txt").exists():
                    info.audio_index = data.get("audio_index", 0)
                    if self.cfg.subtitles.enabled and self.cfg.subtitles.get("film_words", True):
                        from .. import subtitles
                        subtitles.film_words(self.cfg, movie.path, info.audio_index, workdir)
                    log.info("Анализ «%s» взят из кэша: %d сцен, %d моментов",
                             movie.title, len(scenes), len(moments))
                    return Analysis(moments, scenes, info, workdir, subs, data.get("cuts", []),
                                    workdir / "story.txt")

        from .. import subtitles

        info.audio_index = subtitles.pick_audio_track(self.cfg, movie.path, info, workdir)
        if len(info.audio_langs) > 1:
            log.info("Звуковых дорожек %d, беру №%d", len(info.audio_langs), info.audio_index)
        signals_file = workdir / "signals.npz"
        if signals_file.exists():
            sig = dict(np.load(signals_file))
        else:
            log.info("Анализирую «%s» (%s, %dx%d) — это займёт несколько минут",
                     movie.title, ffmpeg.fmt_time(info.duration), info.width, info.height)
            sig = self._video_signals(movie.path)
            if len(sig["motion"]) < 20:
                raise RuntimeError(f"слишком короткое или нечитаемое видео: {movie.path}")
            sig["loud"] = self._audio_rms(movie.path, len(sig["motion"]), info.has_audio, info.audio_index)
            np.savez_compressed(signals_file, **sig)

        feats = self._features(sig)
        moments = self._find_moments(feats, info.duration)
        scenes = self._find_scenes(feats, info.duration)
        cuts = [round(float(t), 2) for t in feats["t"][feats["is_cut"]]]
        transcript = subtitles.transcribe_full(self.cfg, movie.path, info.audio_index, workdir) \
            if self.cfg.subtitles.enabled else []
        if subs:
            log.info("Субтитры фильма: %d реплик", len(subs))
        for m in moments:
            m.speech = self._speech(transcript, subs, m.peak - 4, m.peak + 4)[:400]
        for sc in scenes:
            sc.speech = self._speech(transcript, subs, sc.start, sc.end)[:600]
        self._extract_keyframes(movie.path, moments, workdir)
        self._scene_previews(movie.path, scenes, workdir)
        story = workdir / "story.txt"
        story.write_text(self._story_map(movie, feats, transcript, subs, scenes, info.duration), encoding="utf-8")
        cache.write_text(json.dumps({"version": ANALYSIS_VERSION, "params": self._params(),
                                     "audio_index": info.audio_index, "cuts": cuts,
                                     "moments": [asdict(m) for m in moments],
                                     "scenes": [asdict(sc) for sc in scenes]}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        log.info("Найдено %d сцен-кандидатов и %d пиковых моментов; карта сюжета: %s",
                 len(scenes), len(moments), story)
        return Analysis(moments, scenes, info, workdir, subs, cuts, story)

    @staticmethod
    def _speech(transcript: list[dict], subs, a: float, b: float) -> str:
        spoken = " ".join(s["text"] for s in transcript if s["end"] > a and s["start"] < b)
        foreign = " ".join(t for x, y, t in subs if y > a and x < b)
        return spoken + (f" [перевод иностранной речи: {foreign}]" if foreign else "")

    def _story_map(self, movie: Movie, f: dict, transcript: list[dict], subs, scenes: list[Scene],
                   duration: float) -> str:
        """Фильм как сценарий для куратора: блоки по 10 с — время, накал, темп монтажа, реплики.
        Тихие блоки без слов схлопываются, чтобы текст оставался компактным."""
        fps = f["fps"]
        I, is_cut = f["intensity"], f["is_cut"]
        scene_at = {int(sc.start // 10): sc for sc in scenes}
        lines = [f"Карта сюжета фильма «{movie.title}» ({ffmpeg.fmt_time(duration)}).",
                 "Формат: [время] накал ⚡ (0–3) ✂ частые склейки | реплики. "
                 "«[перевод: …]» — перевод иностранной речи из субтитров. ◆ — начало сцены-кандидата.", ""]
        quiet_from = None
        for b0 in range(0, int(duration), 10):
            b1 = b0 + 10
            seg = I[int(b0 * fps): int(b1 * fps)]
            if not len(seg):
                break
            m = float(np.clip(seg, -5, 10).mean())
            level = 0 if m < 0.4 else 1 if m < 1.0 else 2 if m < 1.8 else 3
            n_cuts = int(is_cut[int(b0 * fps): int(b1 * fps)].sum())
            said = [s["text"] for s in transcript if b0 <= s["start"] < b1]
            said += [f"[перевод: {t}]" for a, b, t in subs if b0 <= a < b1]
            sc = scene_at.get(b0 // 10)
            if not said and level == 0 and not sc:
                quiet_from = b0 if quiet_from is None else quiet_from
                continue
            if quiet_from is not None:
                lines.append(f"[{ffmpeg.fmt_time(quiet_from)}–{ffmpeg.fmt_time(b0)}] … без слов, спокойно")
                quiet_from = None
            mark = f"◆ сцена-кандидат #{sc.id} ({ffmpeg.fmt_time(sc.start)}–{ffmpeg.fmt_time(sc.end)}) " if sc else ""
            lines.append(f"[{ffmpeg.fmt_time(b0)}] {mark}{'⚡' * level}{' ✂' if n_cuts >= 4 else ''} | "
                         + " ".join(said))
        return "\n".join(lines) + "\n"

    # ---------- признаки ----------
    def _features(self, v: dict[str, np.ndarray]) -> dict:
        fps = float(self.a.sample_fps)
        loud = v["loud"]
        n = len(loud)

        # склейки: резкое изменение гистограммы
        cut_thr = max(0.35, float(np.percentile(v["cut"], 99)) * 0.6)
        is_cut = v["cut"] > cut_thr
        cut_rate = _smooth(is_cut.astype(np.float32), int(6 * fps)) * fps * 60  # склеек в минуту

        loud_s = _smooth(loud, int(1.5 * fps))
        motion_s = _smooth(v["motion"], int(1.5 * fps))
        # дроп: громкость следующих 2 с против предыдущих 5 с
        past = _smooth(loud, int(5 * fps))
        shift_past = np.concatenate([np.full(int(3.5 * fps), past[0]), past[:-int(3.5 * fps)]])
        future = _smooth(loud, int(2 * fps))
        shift_fut = np.concatenate([future[int(1 * fps):], np.full(int(1 * fps), future[-1])])
        drop = np.clip(shift_fut - shift_past, 0, None)

        bright = _smooth(v["bright"], int(2 * fps))
        z_loud, z_motion = _robust_z(loud_s), _robust_z(motion_s)
        z_cuts, z_drop = _robust_z(cut_rate), _robust_z(drop)
        z_sat = _robust_z(_smooth(v["sat"], int(2 * fps)))
        z_bright = _robust_z(bright)
        intensity = (0.32 * z_loud + 0.24 * z_motion + 0.18 * z_cuts + 0.14 * z_drop
                     + 0.06 * z_sat + 0.06 * np.clip(z_bright, -2, 1))

        # штрафы: тёмная «каша» (на телефоне не читается), чёрные экраны, вступление и титры
        intensity = intensity - np.clip(0.12 - bright, 0, None) * 25
        intensity[bright < 0.05] -= 10
        lo = int(n * float(self.a.skip_start_pct) / 100)
        hi = int(n * (1 - float(self.a.skip_end_pct) / 100))
        intensity[:lo] -= 100
        intensity[hi:] -= 100
        return {"fps": fps, "t": np.arange(n) / fps, "intensity": intensity, "loud_s": loud_s,
                "motion_s": motion_s, "cut_rate": cut_rate, "drop": drop, "z_drop": z_drop,
                "z_loud": z_loud, "is_cut": is_cut}

    # ---------- пиковые моменты (для общего эдита) ----------
    def _find_moments(self, f: dict, duration: float) -> list[Moment]:
        fps = f["fps"]
        t, intensity, is_cut = f["t"], f["intensity"], f["is_cut"]
        n = len(t)
        max_c = int(self.a.max_candidates)
        gap = int(float(self.a.min_gap_sec) * fps)
        taken = np.zeros(n, bool)
        peaks: list[int] = []

        def take(score: np.ndarray, limit: int) -> None:
            for i in np.argsort(-score):
                if len(peaks) >= limit or score[i] < -5:
                    break
                if taken[i]:
                    continue
                peaks.append(int(i))
                taken[max(0, i - gap): i + gap] = True

        # жадный выбор пиков из двух рейтингов: общая интенсивность и «дропы» (саспенс -> удар)
        take(intensity, int(max_c * 0.7))
        take(np.where(intensity > -5, f["z_drop"] + 0.3 * f["z_loud"], -100), max_c)

        cut_idx = np.flatnonzero(is_cut)
        moments: list[Moment] = []
        for i in sorted(peaks):
            before = cut_idx[cut_idx <= i]
            after = cut_idx[cut_idx > i]
            start = t[before[-1]] if len(before) and t[i] - t[before[-1]] <= 6 else t[i] - 3
            end = t[after[0]] if len(after) and t[after[0]] - t[i] <= 6 else t[i] + 3
            if end - start < 2.5:  # очень частый монтаж — расширяем окно
                start, end = t[i] - 2.5, t[i] + 2.5
            start, end = max(0.0, float(start)), min(duration - 0.5, float(end))
            moments.append(Moment(
                id=len(moments) + 1, peak=float(t[i]), start=round(start, 2), end=round(end, 2),
                score=round(float(intensity[i]), 3), loudness=round(float(f["loud_s"][i]), 1),
                motion=round(float(f["motion_s"][i]), 4), cut_rate=round(float(f["cut_rate"][i]), 1),
                drop=round(float(f["drop"][i]), 1),
            ))
        return moments

    # ---------- цельные сцены (для основных эдитов) ----------
    def _find_scenes(self, f: dict, duration: float) -> list[Scene]:
        fps = f["fps"]
        I, t = f["intensity"], f["t"]
        n = len(I)
        min_len = int(float(self.a.scene_min_sec) * fps)
        max_len = int(float(self.a.scene_max_sec) * fps)

        # возможные границы сцены: склейки, а на длинных планах — сетка каждые 4 с
        bounds = set(np.flatnonzero(f["is_cut"]).tolist()) | {0, n - 1}
        prev = 0
        for c in sorted(bounds):
            if c - prev > 8 * fps:
                bounds.update(range(prev + int(4 * fps), c, int(4 * fps)))
            prev = c
        B = np.array(sorted(bounds))
        csum = np.concatenate([[0.0], np.cumsum(I)])

        cands = []
        for i, a in enumerate(B):
            for b in B[i + 1:]:
                length = b - a
                if length < min_len:
                    continue
                if length > max_len:
                    break
                mean = (csum[b] - csum[a]) / length
                if mean < -1.5:  # вступление, титры, темнота
                    continue
                k = max(1, length // 5)
                top = float(np.partition(I[a:b], -k)[-k:].mean())
                half = length // 2
                build = (csum[b] - csum[a + half]) / (length - half) - (csum[a + half] - csum[a]) / half
                # средняя сила + яркие пики + нарастание к концу + бонус за длину ~24 с (цельная сцена, не обрывок)
                score = mean + 0.5 * top + 0.25 * float(np.clip(build, -1, 2)) + 0.8 * min(1.0, length / (24 * fps))
                cands.append((score, int(a), int(b)))
        cands.sort(reverse=True)

        occupied = np.zeros(n + 1, bool)
        sep = int(10 * fps)
        scenes: list[Scene] = []
        for score, a, b in cands:
            if occupied[max(0, a - sep): b + sep].any():
                continue
            occupied[a:b] = True
            peak = a + int(np.argmax(I[a:b]))
            scenes.append(Scene(id=0, start=round(float(t[a]), 2), end=round(min(float(t[b]), duration - 0.5), 2),
                                peak=round(float(t[peak]), 2), score=round(float(score), 3)))
            if len(scenes) >= int(self.a.max_scenes):
                break
        scenes.sort(key=lambda sc: sc.start)
        for i, sc in enumerate(scenes, 1):
            sc.id = i
        return scenes

    def _extract_keyframes(self, path: Path, moments: list[Moment], workdir: Path) -> None:
        kf_dir = workdir / "keyframes"
        kf_dir.mkdir(exist_ok=True)
        for m in moments:
            out = kf_dir / f"m{m.id:03d}.jpg"
            # кадр чуть после пика: в момент удара часто смаз
            ffmpeg.run(["-ss", f"{m.peak + 0.3:.2f}", "-i", path, "-frames:v", "1",
                        "-vf", "scale=640:-2", "-q:v", "4", out])
            m.keyframe = str(out)

    def _scene_previews(self, path: Path, scenes: list[Scene], workdir: Path) -> None:
        """Четыре кадра сцены (начало, середина, пик, конец) в одной картинке 2x2 — для куратора."""
        pv_dir = workdir / "scenes"
        pv_dir.mkdir(exist_ok=True)
        for sc in scenes:
            dur = sc.end - sc.start
            times = sorted([sc.start + dur * 0.1, sc.start + dur * 0.4, min(sc.peak + 0.3, sc.end - 0.5),
                            sc.start + dur * 0.9])
            frames = []
            for k, ts in enumerate(times):
                out = pv_dir / f"s{sc.id:03d}_{k}.jpg"
                ffmpeg.run(["-ss", f"{ts:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=400:-2", "-q:v", "4", out])
                frames.append(out)
            grid = pv_dir / f"s{sc.id:03d}.jpg"
            args: list = []
            for fr in frames:
                args += ["-i", fr]
            ffmpeg.run([*args, "-filter_complex", "[0][1][2][3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0",
                        "-q:v", "4", grid])
            for fr in frames:
                fr.unlink(missing_ok=True)
            sc.preview = str(grid)
