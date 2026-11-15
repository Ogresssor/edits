"""Отдельная партия непрерывных диалогов из локального кэша, без публикации.

python tools/prepare_dialogue.py --count 3
python tools/prepare_dialogue.py --count 3 --render
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from editbot import ffmpeg
from editbot.arcs import hook_score


def candidates(movie: Path):
    st = movie.stat()
    digest = hashlib.sha1(f"{movie.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]
    cache = ROOT / "work" / digest
    if not (cache / "words.json").exists():
        return []
    data = json.loads((cache / "words.json").read_text(encoding="utf-8"))
    words = data["words"]
    old = json.loads((cache / "arcs.json").read_text(encoding="utf-8")) if (cache / "arcs.json").exists() else {}
    used = list(old.get("used", []))
    for arc in old.get("arcs", []):
        used.extend([line[:2] for line in arc["lines"]])
        used.extend([[t, t + 2] for t in arc.get("broll", []) + arc.get("finale", [])])
    result = []
    for i, (a, _, text) in enumerate(words):
        if a < 90 or a > words[-1][1] * .80:
            continue
        # Начало после законченной фразы или настоящей паузы.
        if i and not (re.search(r"[.!?]$", words[i-1][2]) or a - words[i-1][1] >= .7):
            continue
        opening = " ".join(w[2] for w in words[i:i+9])
        score = hook_score(opening)
        if score < .8:
            continue
        ends = [j for j in range(i + 1, min(i + 130, len(words)))
                if 20 <= words[j][1] - a <= 34 and re.search(r"[.!?]$", words[j][2])]
        if not ends:
            continue
        j = ends[-1]
        b = words[j][1]
        if any(a - 2 < y and b + 2 > x for x, y in used):
            continue
        selected = words[i:j+1]
        if sum(w[1]-w[0] for w in selected) / (b-a) < .45:
            continue
        result.append(dict(movie=str(movie), start=round(max(0, a-.12), 3), end=round(b+.22, 3),
                           score=score, audio_index=data.get("audio_index", 0), words=selected,
                           text=" ".join(w[2] for w in selected)))
    return result


def stamp(t):
    ms = round(max(0, t) * 1000)
    return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"


def render(item, folder, number):
    stem = f"dialogue_{number:02}"
    srt = folder / f"{stem}.srt"
    lines = []
    words = item["words"]
    for i in range(0, len(words), 5):
        group = words[i:i+5]
        lines.append(f"{len(lines)+1}\n{stamp(group[0][0]-item['start'])} --> "
                     f"{stamp(group[-1][1]-item['start'])}\n{' '.join(w[2] for w in group)}\n")
    srt.write_text("\n".join(lines), encoding="utf-8")
    target = folder / f"{stem}.mp4"
    if target.exists():
        raise FileExistsError(f"Ролик уже существует: {target}")
    graph = ("[0:v]split=2[bg][fg];"
             "[bg]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,"
             "boxblur=24:2,eq=brightness=-0.16[back];"
             "[fg]scale=720:1280:force_original_aspect_ratio=decrease[front];"
             "[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1,"
             f"subtitles={srt.name}:force_style='FontName=Arial,FontSize=19,Outline=2,"
             "Alignment=2,MarginV=68,MarginL=26,MarginR=26'[v]")
    ffmpeg.run(["-ss", str(item["start"]), "-i", str(item["movie"]),
                "-t", str(round(item["end"]-item["start"], 3)), "-filter_complex", graph,
                "-map", "[v]", "-map", f"0:a:{item['audio_index']}",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264", "-preset", "fast",
                "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(target)], cwd=folder, timeout=600)
    info = ffmpeg.probe(target)
    expected = item["end"]-item["start"]
    if not info.has_audio or (info.width, info.height) != (720, 1280) or abs(info.duration-expected) > .5:
        raise RuntimeError(f"Не пройдена проверка {target}")
    return dict(file=target.name, duration=info.duration, width=info.width, height=info.height,
                has_audio=info.has_audio)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--plan", type=Path, help="смонтировать проверенный candidates.json без повторного отбора")
    parser.add_argument("--only", default="Peaky Blinders")
    parser.add_argument("--output", type=Path, default=ROOT / "review" / "dialogue_v1")
    args = parser.parse_args()
    if not 1 <= args.count <= 20:
        parser.error("--count должен быть от 1 до 20")
    pool = []
    for movie in (ROOT / "library").rglob("*"):
        if movie.suffix.lower() in {".mkv", ".mp4", ".m4v"} and args.only.casefold() in str(movie).casefold():
            pool.extend(candidates(movie))
    chosen = []
    for c in sorted(pool, key=lambda c: -c["score"]):
        if all(c["movie"] != p["movie"] or c["end"]+15 < p["start"] or c["start"]-15 > p["end"] for p in chosen):
            chosen.append(c)
        if len(chosen) >= args.count:
            break
    if args.plan:
        chosen = json.loads(args.plan.read_text(encoding="utf-8"))
        for item in chosen:
            if not Path(item["movie"]).is_file() or not 0 <= item["start"] < item["end"]:
                raise ValueError("Некорректный источник или границы в плане")
    if not chosen:
        raise SystemExit("Нет новых кандидатов в локальном кэше")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "candidates.json"
    if args.render and any(args.output.glob("dialogue_*.mp4")):
        raise SystemExit("В папке уже есть ролики; задайте новую --output")
    manifest.write_text(json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")
    checks = []
    for n, item in enumerate(chosen, 1):
        print(f"{n}. {Path(item['movie']).name} {item['start']}–{item['end']}\n{item['text']}\n", flush=True)
        if args.render:
            checks.append(render(item, args.output, n))
    if args.render:
        (args.output / "checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
