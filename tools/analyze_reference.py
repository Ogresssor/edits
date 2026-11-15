"""Разбор эталонных эдитов: склейки, темп, связь с битом, зум/тряска, вспышки, цвет, речь, раскладка кадра.

    python tools/analyze_reference.py reference/fyzenix

Для каждого ролика пишет <имя>.analysis.json и лист кадров <имя>.sheet.jpg, в конце — сводку summary.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import imageio_ffmpeg  # noqa: E402

FF = imageio_ffmpeg.get_ffmpeg_exe()
AW, AH = 240, 0          # ширина кадра для анализа


def probe(path: Path) -> tuple[int, int, float, float]:
    out = subprocess.run([FF, "-i", str(path)], capture_output=True, text=True, encoding="utf-8",
                         errors="ignore").stderr
    import re
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5}).*?([\d.]+) fps", out)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3])
    return int(m[1]), int(m[2]), float(m[3]), dur


def frames(path: Path, w: int, h: int, fps: float):
    p = subprocess.Popen([FF, "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}", "-pix_fmt", "bgr24",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    n = w * h * 3
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        yield np.frombuffer(b, np.uint8).reshape(h, w, 3)
    p.wait()


def audio(path: Path, sr: int = 22050) -> np.ndarray:
    b = subprocess.run([FF, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True).stdout
    return np.frombuffer(b, np.float32)


def global_motion(prev: np.ndarray, cur: np.ndarray, orb, bf) -> tuple[float, float, float, float]:
    """(dx, dy, scale, rot) — сдвиг/зум/поворот кадра относительно предыдущего."""
    k1, d1 = orb.detectAndCompute(prev, None)
    k2, d2 = orb.detectAndCompute(cur, None)
    if d1 is None or d2 is None or len(k1) < 12 or len(k2) < 12:
        return 0.0, 0.0, 1.0, 0.0
    m = bf.match(d1, d2)
    if len(m) < 12:
        return 0.0, 0.0, 1.0, 0.0
    a = np.float32([k1[x.queryIdx].pt for x in m])
    b = np.float32([k2[x.trainIdx].pt for x in m])
    M, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if M is None or inl.sum() < 10:
        return 0.0, 0.0, 1.0, 0.0
    s = float(np.hypot(M[0, 0], M[1, 0]))
    return float(M[0, 2]), float(M[1, 2]), s, float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))


def analyze(path: Path) -> dict:
    W, H, fps, dur = probe(path)
    w = AW
    h = int(round(H * w / W / 2)) * 2
    orb = cv2.ORB_create(400)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    luma, sat, sharp, hd, motion, rows, colmean = [], [], [], [], [], [], []
    prev_h = prev_g = None
    thumbs = {}
    for i, f in enumerate(frames(path, w, h, fps)):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        luma.append(float(g.mean()))
        sat.append(float(hsv[..., 1].mean()))
        sharp.append(float(cv2.Laplacian(g, cv2.CV_32F).var()))
        rows.append(g.mean(axis=1))
        colmean.append(f.reshape(-1, 3).mean(0))
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        hd.append(cv2.compareHist(prev_h, hist, cv2.HISTCMP_BHATTACHARYYA) if prev_h is not None else 0.0)
        motion.append(global_motion(prev_g, g, orb, bf) if prev_g is not None else (0, 0, 1, 0))
        prev_h, prev_g = hist, g
        if i % int(round(fps)) == 0:
            thumbs[i] = cv2.resize(f, (144, int(144 * h / w)))
    n = len(luma)
    t = np.arange(n) / fps
    luma, sat, sharp, hd = map(np.array, (luma, sat, sharp, hd))
    motion = np.array(motion)
    rows = np.array(rows)

    # раскладка: строки, которые почти всегда чёрные — поля
    row_mean = rows.mean(0)
    row_max = np.percentile(rows, 95, axis=0)
    content = np.nonzero(row_max > 22)[0]
    layout = {"content_top_pct": round(100 * content.min() / h, 1) if len(content) else 0,
              "content_bottom_pct": round(100 * (content.max() + 1) / h, 1) if len(content) else 100}

    # склейки: резкий скачок гистограммы, выше локального фона
    cuts = []
    for i in range(1, n):
        loc = np.median(hd[max(0, i - 8):i + 8])
        if hd[i] > 0.32 and hd[i] > 3 * loc + 0.08 and (not cuts or i - cuts[-1] >= 3):
            cuts.append(i)
    cut_t = np.array(cuts) / fps
    shots = np.diff(np.r_[0, cut_t, n / fps])

    # вспышки: яркость резко выше соседей
    flashes = [i / fps for i in range(3, n - 3)
               if luma[i] > np.median(luma[max(0, i - 15):i + 15]) + 35 and luma[i] >= luma[i - 1]
               and luma[i] >= luma[i + 1]]
    # зум-импульсы и тряска
    dx, dy, sc, rot = motion.T
    zoom_rate = (sc - 1) * fps                       # относительное изменение масштаба в секунду
    shake = np.hypot(dx, dy)
    shake_hi = np.abs(shake - cv2.GaussianBlur(shake.reshape(-1, 1), (1, 9), 0).ravel())
    blur_dips = [i / fps for i in range(2, n - 2)
                 if sharp[i] < 0.35 * np.median(sharp[max(0, i - 30):i + 30])
                 and sharp[i] == sharp[max(0, i - 3):i + 4].min()]

    # аудио: бит и онсеты
    import librosa
    y = audio(path)
    sr = 22050
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="time")
    tempo = float(np.atleast_1d(tempo)[0])

    def near(ts, ref):
        if len(ref) == 0 or len(ts) == 0:
            return []
        return [float(np.min(np.abs(ref - x))) for x in ts]

    cut_beat = near(cut_t, beats)
    cut_onset = near(cut_t, onsets)
    beat_period = 60 / tempo if tempo else 0.5
    # случайный уровень совпадения для сравнения: равномерные точки
    rnd = np.random.default_rng(0).uniform(0, n / fps, 400)
    rnd_beat = near(rnd, beats)

    # речь (диалог фильма поверх музыки?)
    speech = []
    try:
        if os.environ.get("NO_SPEECH"): raise RuntimeError("skip")
        from faster_whisper import WhisperModel
        global _WM
        if "_WM" not in globals():
            _WM = WhisperModel("small", device="cpu", compute_type="int8")
        segs, info = _WM.transcribe(str(path), vad_filter=True, beam_size=1)
        speech = [{"s": round(s.start, 2), "e": round(s.end, 2), "text": s.text.strip()} for s in segs
                  if s.no_speech_prob < 0.6]
        lang = info.language
    except Exception as e:
        lang = f"err {e}"

    # интенсивность по времени: склеек в секунду в каждой четверти
    q = np.linspace(0, n / fps, 5)
    cut_rate_q = [round(float(((cut_t >= q[k]) & (cut_t < q[k + 1])).sum() / (q[k + 1] - q[k])), 2)
                  for k in range(4)]

    res = {
        "file": path.name, "size": [W, H], "fps": fps, "duration": round(n / fps, 2),
        "layout": layout,
        "cuts": len(cuts), "cuts_per_sec": round(len(cuts) / (n / fps), 2), "cut_rate_by_quarter": cut_rate_q,
        "shot_len": {"median": round(float(np.median(shots)), 2), "p10": round(float(np.percentile(shots, 10)), 2),
                     "p90": round(float(np.percentile(shots, 90)), 2), "max": round(float(shots.max()), 2)},
        "longest_shots": [[round(float(np.r_[0, cut_t][k]), 1), round(float(shots[k]), 1)]
                          for k in np.argsort(-shots)[:4]],
        "tempo_bpm": round(tempo, 1), "beat_period": round(beat_period, 3),
        "cut_to_beat_median_ms": round(1000 * float(np.median(cut_beat)), 1) if cut_beat else None,
        "random_to_beat_median_ms": round(1000 * float(np.median(rnd_beat)), 1) if rnd_beat else None,
        "cuts_on_beat_pct": round(100 * float(np.mean(np.array(cut_beat) < 0.05)), 1) if cut_beat else None,
        "cuts_on_onset_pct": round(100 * float(np.mean(np.array(cut_onset) < 0.05)), 1) if cut_onset else None,
        "flashes": len(flashes), "flash_times": [round(x, 2) for x in flashes[:30]],
        "blur_dips": len(blur_dips),
        "zoom": {"frames_zooming_pct": round(100 * float(np.mean(np.abs(zoom_rate) > 0.15)), 1),
                 "max_zoom_rate": round(float(np.percentile(np.abs(zoom_rate), 99)), 2)},
        "shake_frames_pct": round(100 * float(np.mean(shake_hi > 2.0)), 1),
        "rotation_frames_pct": round(100 * float(np.mean(np.abs(rot) > 0.4)), 1),
        "luma_mean": round(float(luma.mean()), 1), "sat_mean": round(float(sat.mean()), 1),
        "lang": lang, "speech_sec": round(sum(s["e"] - s["s"] for s in speech), 1), "speech": speech,
        "cut_times": [round(x, 3) for x in cut_t.tolist()],
        "beats": [round(float(x), 3) for x in beats],
    }
    # лист кадров (1 кадр в секунду) + отметки склеек
    ks = sorted(thumbs)
    cols = 10
    th, tw = thumbs[ks[0]].shape[:2]
    rows_n = (len(ks) + cols - 1) // cols
    sheet = np.zeros((rows_n * (th + 18), cols * tw, 3), np.uint8)
    for j, k in enumerate(ks):
        r, c = divmod(j, cols)
        sheet[r * (th + 18):r * (th + 18) + th, c * tw:(c + 1) * tw] = thumbs[k]
        cv2.putText(sheet, f"{k / fps:.0f}s", (c * tw + 4, r * (th + 18) + th + 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (255, 255, 255), 1)
    cv2.imwrite(str(path.with_suffix(".sheet.jpg")), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    np.savez_compressed(path.with_suffix(".curves.npz"), luma=luma, sat=sat, sharp=sharp, hd=hd, motion=motion,
                        fps=fps)
    path.with_suffix(".analysis.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main():
    folder = Path(sys.argv[1])
    out = []
    for p in sorted(folder.glob("*.mp4")):
        print("…", p.name, flush=True)
        r = analyze(p)
        out.append(r)
        print(f"  {r['duration']}s {r['size']} cuts {r['cuts']} ({r['cuts_per_sec']}/s) shot med {r['shot_len']['median']}"
              f" bpm {r['tempo_bpm']} cut→beat {r['cut_to_beat_median_ms']}ms (rnd {r['random_to_beat_median_ms']})"
              f" flashes {r['flashes']} zoom% {r['zoom']['frames_zooming_pct']} speech {r['speech_sec']}s "
              f"layout {r['layout']}", flush=True)
    (folder / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
