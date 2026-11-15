"""Субтитры для вертикальных эдитов: распознавание речи (Whisper) + .srt фильма -> короткие фразы в ASS.

Стиль под телефон: небольшой жирный шрифт с обводкой, по 2–4 слова на экране, в нижней трети кадра —
выше интерфейса Shorts (кнопки и подпись занимают низ экрана).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import ffmpeg
from .config import Config

log = logging.getLogger("subtitles")


@dataclass
class Word:
    start: float
    end: float
    text: str


def _cuda_ready() -> bool:
    """Видеокарта NVIDIA для Whisper: CUDA-библиотеки (cuBLAS, cuDNN) берём из установленного PyTorch."""
    try:
        import os

        import ctranslate2
        import torch

        lib = Path(torch.__file__).parent / "lib"
        if lib.exists() and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(lib))
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


@lru_cache(maxsize=2)
def _model(size: str):
    from faster_whisper import WhisperModel

    if _cuda_ready():
        log.info("Загружаю модель распознавания речи Whisper «%s» на видеокарту (первый раз скачивается)…", size)
        return WhisperModel(size, device="cuda", compute_type="float16")
    log.info("Загружаю модель распознавания речи Whisper «%s» (процессор; первый раз скачивается)…", size)
    return WhisperModel(size, device="cpu", compute_type="int8")


# ---------------------------------------------------------------- пословная расшифровка всего фильма
WORDS_VERSION = 1
_FILM_WORDS: dict[str, list] = {}       # путь фильма -> [(start, end, text), ...] из words.json


def film_words(cfg: Config, movie: Path, audio_index: int, workdir: Path) -> list[Word]:
    """Весь фильм по словам (точная модель, один раз; кэш work/<фильм>/words.json). Из неё берут слова
    и выбор моментов, и границы реплик, и субтитры — тайминги везде одни и те же."""
    import json

    model = str(cfg.subtitles.get("film_model", "large-v3"))
    cache = workdir / "words.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("v") == WORDS_VERSION and data.get("model") == model and data.get("audio_index") == audio_index:
            words = [Word(a, b, t) for a, b, t in data["words"]]
            _FILM_WORDS[str(Path(movie).resolve())] = words
            return words
    wav = workdir / "full_audio.wav"
    log.info("Расшифровываю весь фильм по словам (Whisper «%s») — делается один раз…", model)
    _extract_wav(movie, 0, None, audio_index, wav)
    words: list[Word] = []
    segments_out = []
    try:
        segments, meta = _model(model).transcribe(
            str(wav), language=cfg.subtitles.language, word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400}, beam_size=5,
            condition_on_previous_text=False, initial_prompt=_prompt(str(cfg.subtitles.language)))
        next_log = 600.0
        for seg in segments:
            if seg.no_speech_prob > 0.7:
                continue
            seg_words = [Word(round(w.start, 3), round(w.end, 3), w.word.strip()) for w in (seg.words or [])
                         if w.word.strip() and w.probability > 0.15]
            words += seg_words
            if seg.text.strip():
                segments_out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
            if seg.end > next_log:
                log.info("  расшифровано %d мин из %d", int(seg.end // 60), int(meta.duration // 60))
                next_log += 600
    finally:
        wav.unlink(missing_ok=True)
    cache.write_text(json.dumps({"v": WORDS_VERSION, "model": model, "audio_index": audio_index,
                                 "words": [[w.start, w.end, w.text] for w in words]}, ensure_ascii=False),
                     encoding="utf-8")
    # карта сюжета для куратора — из той же расшифровки (без второго прохода)
    (workdir / "transcript.json").write_text(
        json.dumps({"model": model, "audio_index": audio_index, "segments": segments_out}, ensure_ascii=False,
                   indent=0), encoding="utf-8")
    log.info("Расшифровка готова: %d слов, %d реплик", len(words), len(segments_out))
    _FILM_WORDS[str(Path(movie).resolve())] = words
    return words


def unload_models() -> None:
    """Выгрузить модели Whisper из памяти (следующая расшифровка загрузит заново)."""
    _model.cache_clear()
    import gc
    gc.collect()


_PUNCT_PROMPT = {
    "ru": "Привет! Ну, как дела? Всё хорошо, спасибо. Пойдём, нам пора.",
    "en": "Hello! Well, how are you? I'm fine, thanks. Let's go, it's time.",
}
_VOCAB: list[str] = []
_CACHE: dict = {}   # имена персонажей от агента-исследователя


def set_vocabulary(names: list[str]) -> None:
    """Имена героев фильма — Whisper узнаёт их по подсказке (иначе «Дакаши» вместо «Такаши»)."""
    _VOCAB[:] = [n for n in names if n][:30]


_FILM_FIXES: dict[str, str] = {}


def set_film(cfg: Config, title: str) -> None:
    """Исправления распознавания только для этого фильма (subtitles.film_fixes: «Форсаж»: {«Том»: «Дом»})."""
    _FILM_FIXES.clear()
    for key, fixes in ((cfg._data.get("subtitles") or {}).get("film_fixes") or {}).items():
        if key.lower() in title.lower():
            _FILM_FIXES.update(fixes or {})


def fix_text(cfg: Config, t: str) -> str:
    """Общие (subtitles.fixes) и для текущего фильма исправления частых ошибок распознавания."""
    for wrong, right in {**((cfg._data.get("subtitles") or {}).get("fixes") or {}), **_FILM_FIXES}.items():
        t = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, t)
    return t


def _prompt(lang: str) -> str | None:
    base = _PUNCT_PROMPT.get(lang)
    if _VOCAB and base:
        return f"{base} {', '.join(_VOCAB)}."
    return base


def _extract_wav(movie: Path, start: float, dur: float | None, audio_index: int, wav: Path) -> None:
    args = ["-ss", f"{max(0.0, start):.3f}"]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    ffmpeg.run([*args, "-i", movie, "-map", f"0:a:{audio_index}", "-ac", "1", "-ar", "16000", wav])


def transcribe(cfg: Config, movie: Path, start: float, dur: float, workdir: Path,
               audio_index: int = 0, model: str | None = None) -> list[Word]:
    """Слова с таймкодами (абсолютное время фильма) на участке [start, start+dur]."""
    s = cfg.subtitles
    film = _FILM_WORDS.get(str(Path(movie).resolve()))
    local = dur <= float(s.get("local_max_sec", 40))
    if film is not None and model is None and not local:
        # длинный участок (выбор моментов) — из расшифровки всего фильма; точность до долей секунды не нужна
        return [w for w in film if w.end > start and w.start < start + dur]
    if film is not None and model is None:
        # короткий участок (реплика в ролике) — заново по самому куску: на длинной записи Whisper сдвигает
        # тайминги слов до ~1 с, на коротком куске они точные — субтитры совпадают с голосом
        model = str(s.get("film_model", "large-v3"))
    key = (str(movie), round(start, 2), round(dur, 2), audio_index, model or str(s.model), tuple(_VOCAB))
    if key in _CACHE:          # куратор и монтажёр слушают одни и те же куски — второй раз не распознаём
        return list(_CACHE[key])
    wav = workdir / f"speech_{int(start * 1000)}.wav"
    _extract_wav(movie, start, dur, audio_index, wav)
    def run(vad: bool) -> list[Word]:
        segments, _ = _model(model or str(s.model)).transcribe(
            # без отсечения тишины: склейка кусков после VAD — источник сдвига таймингов
            str(wav), language=s.language, word_timestamps=True, vad_filter=vad,
            condition_on_previous_text=False, beam_size=5,
            # образец текста со знаками препинания: без него Whisper на коротких кусках иногда пишет
            # всё строчными и без точек — и фразы для экрана режутся где попало
            initial_prompt=_prompt(str(s.language)),
        )
        out = []
        for seg in segments:
            if seg.no_speech_prob > 0.7:
                continue
            for w in seg.words or []:
                text = w.word.strip()
                if text and w.probability > 0.15:
                    out.append(Word(start + w.start, start + w.end, text))
        return out

    try:
        words = run(vad=not local)
        if film is not None and local:
            # кусок под музыку Whisper иногда «не слышит» целиком (реплика без субтитров) — сверяемся с
            # расшифровкой всего фильма: если там слов намного больше, пробуем с отсечением тишины,
            # а если и так мало — берём слова из расшифровки фильма (тайминги чуть грубее, но текст есть)
            ref = [w for w in film if w.end > start and w.start < start + dur]
            if len(words) < 0.5 * len(ref):
                again = run(vad=True)
                words = again if len(again) >= 0.5 * len(ref) else ref
                log.info("  %s: локально распознано мало слов — взял %s", ffmpeg.fmt_time(start),
                         "повтор с отсечением тишины" if words is again else "расшифровку всего фильма")
        _CACHE[key] = list(words)
        return words
    finally:
        wav.unlink(missing_ok=True)


def pick_audio_track(cfg: Config, movie: Path, info: ffmpeg.MediaInfo, workdir: Path) -> int:
    """Номер звуковой дорожки на нужном языке: по меткам файла, а если их нет — Whisper слушает по 40 с каждой."""
    if len(info.audio_langs) <= 1:
        return 0
    want = str(cfg.subtitles.language)
    tags = {"ru": ("rus", "ru"), "en": ("eng", "en")}.get(want, (want,))
    # среди дорожек нужного языка — дубляж (у закадровых озвучек голоса героев не слышны, звучит хуже)
    titles = info.audio_titles or [""] * len(info.audio_langs)
    for i, (lang, title) in enumerate(zip(info.audio_langs, titles)):
        if lang in tags and re.search(r"дубляж|дублир|\bdub", title, re.I):
            return i
    for i, lang in enumerate(info.audio_langs):
        if lang in tags:
            return i
    # слушаем каждую дорожку в трёх местах (в одном может играть музыка без слов)
    best, best_p = 0, -1.0
    for i in range(len(info.audio_langs)):
        total, heard = 0.0, []
        for frac in (0.25, 0.45, 0.65):
            wav = workdir / f"lang_{i}.wav"
            try:
                _extract_wav(movie, info.duration * frac, 30, i, wav)
                _, meta = _model("small").transcribe(str(wav), vad_filter=True, beam_size=1)
                total += meta.language_probability if meta.language == want else 0.0
                heard.append(meta.language)
            except Exception as e:
                log.warning("  не удалось определить язык дорожки %d: %s", i, e)
            finally:
                wav.unlink(missing_ok=True)
        log.info("  звуковая дорожка %d: %s", i, ", ".join(heard) or "?")
        if total > best_p:
            best, best_p = i, total
    return best


def transcribe_full(cfg: Config, movie: Path, audio_index: int, workdir: Path) -> list[dict]:
    """Расшифровка всего фильма (для понимания сюжета куратором). Кэшируется в transcript.json.
    Возвращает реплики: [{"start", "end", "text"}, ...]."""
    import json

    if cfg.subtitles.get("film_words", True):
        film_words(cfg, movie, audio_index, workdir)
        return json.loads((workdir / "transcript.json").read_text(encoding="utf-8"))["segments"]
    model = str(cfg.subtitles.story_model)
    cache = workdir / "transcript.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("model") == model and data.get("audio_index") == audio_index:
            return data["segments"]
    wav = workdir / "full_audio.wav"
    log.info("Расшифровываю весь фильм (Whisper «%s») — это ~15 мин, делается один раз…", model)
    _extract_wav(movie, 0, None, audio_index, wav)
    try:
        segments, meta = _model(model).transcribe(
            str(wav), language=cfg.subtitles.language, vad_filter=True, beam_size=1,
            condition_on_previous_text=False,
        )
        out, next_log = [], 600.0
        for seg in segments:
            text = seg.text.strip()
            if text and seg.no_speech_prob < 0.7:
                out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
            if seg.end > next_log:
                log.info("  расшифровано %d мин из %d", int(seg.end // 60), int(meta.duration // 60))
                next_log += 600
    finally:
        wav.unlink(missing_ok=True)
    cache.write_text(json.dumps({"model": model, "audio_index": audio_index, "segments": out},
                                ensure_ascii=False, indent=0), encoding="utf-8")
    log.info("Расшифровка готова: %d реплик", len(out))
    return out


def srt_words(subs: list[tuple[float, float, str]], start: float, end: float) -> list[tuple[float, float, str]]:
    """Реплики из .srt фильма внутри участка (обычно это перевод иностранной речи)."""
    # реплику, начавшуюся до куска, не берём — на экране был бы обрывок чужой фразы
    return [(a, b, t) for a, b, t in subs if a >= start - 0.3 and a < end - 0.3]


_SENT_END = re.compile(r"[.!?…]$")


def snap_window(words: list[Word], start: float, end: float, max_len: float,
                slack: float = 3.0) -> tuple[float, float]:
    """Сдвигает границы сцены, чтобы не резать реплику на полуслове."""
    if not words:
        return start, end
    # начало: если в этот момент звучит фраза — начинаем с начала предложения (не дальше slack назад)
    first = next((i for i, w in enumerate(words) if w.end > start), None)
    if first is not None and words[first].start < start + 0.3:
        j = first
        while (j > 0 and words[j - 1].end > start - slack and not _SENT_END.search(words[j - 1].text)
               and words[j].start - words[j - 1].end < 0.6):
            j -= 1
        start = max(start - slack, min(start, words[j].start - 0.15))
    # конец: если фраза не договорена — продлеваем до конца предложения (не дальше slack вперёд)
    last = max((i for i, w in enumerate(words) if w.start < end), default=None)
    if last is not None and words[last].end > end - 0.6:
        j = last
        while (j + 1 < len(words) and words[j + 1].start < end + slack and not _SENT_END.search(words[j].text)
               and words[j + 1].start - words[j].end < 0.6):
            j += 1
        end = min(end + slack, max(end, words[j].end + 0.35))
    if end - start > max_len:
        end = start + max_len
    return start, end


_FUNCTION_WORDS = {"и", "а", "но", "или", "да", "в", "во", "на", "о", "об", "с", "со", "к", "ко", "у", "по", "за",
                   "из", "от", "до", "для", "без", "при", "про", "не", "ни", "что", "как", "чтобы", "я", "ты", "он",
                   "она", "мы", "вы", "они", "бы", "же", "ли", "то", "это", "the", "a", "to", "of"}


def _text_len(ws: list[Word]) -> int:
    return len(" ".join(w.text for w in ws))


def _split_sentence(ws: list[Word], max_words: int, max_chars: int) -> list[list[Word]]:
    """Делит предложение на фразы для экрана: пополам по смыслу (лучше на запятой), пока не влезет."""
    if len(ws) <= max_words and _text_len(ws) <= max_chars or len(ws) == 1:
        return [ws]

    def cost(i: int) -> float:
        balance = abs(_text_len(ws[:i]) - _text_len(ws[i:]))
        comma = 12 if re.search(r"[,;:—–-]$", ws[i - 1].text) else 0
        lonely = 25 if i == 1 or i == len(ws) - 1 else 0  # не оставляем одно слово отдельно
        # фраза не должна заканчиваться предлогом/союзом: «Стал бы я о / таком врать»
        dangling = 15 if ws[i - 1].text.lower().strip(",") in _FUNCTION_WORDS else 0
        return balance - comma + lonely + dangling

    i = min(range(1, len(ws)), key=cost)
    return _split_sentence(ws[:i], max_words, max_chars) + _split_sentence(ws[i:], max_words, max_chars)


def _chunks(words: list[Word], max_words: int, max_chars: int) -> list[tuple[float, float, str, list[Word]]]:
    # сначала предложения (по знакам конца или паузе в речи), потом каждое — на экранные фразы
    sentences, cur = [], []
    for w in words:
        if cur and w.start - cur[-1].end > 0.6:
            sentences.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w.text):
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)
    out = []
    for s in sentences:
        for part in _split_sentence(s, max_words, max_chars):
            out.append((part[0].start, part[-1].end, " ".join(w.text for w in part), part))
    return out


def _split_line(a: float, b: float, text: str, max_chars: int) -> list[tuple[float, float, str]]:
    """Длинную реплику .srt режем на куски с пропорциональным временем."""
    parts, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            parts.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        parts.append(cur)
    total = sum(len(p) for p in parts) or 1
    out, t = [], a
    for p in parts:
        d = (b - a) * len(p) / total
        out.append((t, t + d, p))
        t += d
    return out


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _pop_line(tokens: list[str], ws: list[Word] | None, a: float, b: float, offset: float,
              colors: list[str], color_start: int, glow: bool) -> str:
    """Одна фраза в стиле эдита: слова «выпрыгивают» по одному, когда звучат (сплющенное -> 120% -> 100%),
    у каждого слова свой неоновый цвет; на выходе фраза «взрывается» и тает.
    glow=True — нижний слой: только цветная размытая обводка (свечение)."""
    dur_ms = int((b - a) * 1000)
    out_ms = max(0, dur_ms - 150)
    parts = []
    for k, tok in enumerate(tokens):
        col = colors[(color_start + k) % len(colors)]
        dt = 0
        if ws and k < len(ws):
            dt = max(0, min(int((ws[k].start - offset - a) * 1000), max(0, dur_ms - 250)))
        exit_ = f"\\t({out_ms},{dur_ms},\\fscx125\\fscy125\\blur8\\alpha&HFF&)"
        if glow:
            tag = (f"{{\\1a&HFF&\\bord16\\blur12\\3c{col}&\\3a&HFF&\\t({dt},{dt + 70},\\3a&H50&)"
                   f"\\fscy40\\t({dt},{dt + 110},\\fscy122)\\t({dt + 110},{dt + 200},\\fscy100)"
                   f"\\t({out_ms},{dur_ms},\\fscx125\\fscy125\\3a&HFF&)}}")
        else:
            tag = (f"{{\\c{col}&\\alpha&HFF&\\t({dt},{dt + 70},\\alpha&H00&)"
                   f"\\fscy40\\t({dt},{dt + 110},\\fscy122)\\t({dt + 110},{dt + 200},\\fscy100)"
                   f"{exit_}}}")
        parts.append(tag + _ass_escape(tok))
    # вся строка: лёгкий «подскок» с поворотом при появлении
    head = "{\\frz-4\\t(0,160,\\frz0)}"
    return head + " ".join(parts)


def _join_hyphens(words: list[Word]) -> list[Word]:
    """«что -нибудь», «Э -э», «О 'Коннор» (Whisper ставит пробел перед дефисом или апострофом, иногда делит
    на два слова) -> слитно."""
    out: list[Word] = []
    for w in words:
        text = re.sub(r"(\w)\s+([-'’])(\w)", r"\1\2\3", w.text)
        if out and re.match(r"\s*[-'’]\w", text) and w.start - out[-1].end < 0.6:
            out[-1] = Word(out[-1].start, w.end, out[-1].text.rstrip() + text.strip())
        else:
            out.append(Word(w.start, w.end, text))
    return out


def _fix_text(cfg: Config, t: str) -> str:
    return fix_text(cfg, t)


def _tiers(ws: list[Word], max_top: int = 26) -> tuple[list[Word], list[Word]]:
    """Делит фразу на две строки: верхняя — мелкая «подводка», нижняя — крупная главная часть."""
    if len(ws) <= 2:
        return [], ws
    total = _text_len(ws)

    def cost(i: int) -> float:
        top = _text_len(ws[:i])
        c = abs(top - 0.45 * total)
        if re.search(r"[,;:—–-]$", ws[i - 1].text):
            c -= 8
        if ws[i - 1].text.lower().strip(",") in _FUNCTION_WORDS:
            c += 10
        if top > max_top:
            c += 50
        return c

    i = min(range(1, len(ws)), key=cost)
    return ws[:i], ws[i:]


def cine_events(cfg: Config, words: list[Word], duration: float, palette: list[str] | None = None) -> list[dict]:
    """Фразы для субтитров в две строки (как build_cine_ass), в виде данных — для After Effects:
    [{"start", "end", "color", "top": [{"t", "text"}...], "main": [...]}], время — от начала ролика."""
    s = cfg.subtitles
    words = _join_hyphens(words)
    events = _chunks(words, int(s.get("cine_max_words", 7)), int(s.get("cine_max_chars", 38)))
    while events and events[0][0] < 0.12 and events[0][2][:1].islower():
        events.pop(0)
    colors = palette or ["#35F2FF", "#FF4FD8", "#FFE14D", "#FF4A3D", "#7CFF5B"]
    out = []
    for i, (a, b, _text, ws) in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else None
        end = min(b + 0.9, nxt + 0.12) if nxt is not None else b + 0.9
        end = min(duration, max(end, a + 0.8))
        a = max(0.0, a)
        if end - a < 0.25:
            continue
        top, main = _tiers(ws)
        tag = lambda seq: [{"t": round(max(0.0, w.start - a), 3), "text": _fix_text(cfg, w.text).upper()} for w in seq]
        out.append({"start": round(a, 3), "end": round(end, 3), "color": colors[len(out) % len(colors)],
                    "top": tag(top), "main": tag(main)})
    return out


def build_cine_ass(cfg: Config, words: list[Word], duration: float, width: int, height: int, path: Path,
                   palette: list[str] | None = None) -> int:
    """Субтитры как в эталонных эдитах: посередине кадра, в две строки — сверху мелкая белая с разрядкой,
    снизу крупная жирная курсивная цветная со свечением. Слова проявляются по одному, когда звучат
    (из размытия), фразы сменяют друг друга растворением. Цвет главной строки — по палитре образа."""
    from .looks import ass_color

    s = cfg.subtitles
    words = _join_hyphens(words)
    events = _chunks(words, int(s.get("cine_max_words", 7)), int(s.get("cine_max_chars", 38)))
    while events and events[0][0] < 0.12 and events[0][2][:1].islower():
        events.pop(0)
    colors = [ass_color(c) for c in (palette or ["#35F2FF", "#FF4FD8", "#FFE14D", "#FF4A3D", "#7CFF5B"])]
    size1 = int(width * float(s.get("cine_top_pct", 3.9)) / 100)
    size2 = int(width * float(s.get("cine_main_pct", 7.0)) / 100)
    y1 = int(height * float(s.get("cine_y_pct", 56)) / 100)
    y2 = y1 + int(size1 * 0.62 + size2 * 0.72)
    cx = width // 2
    max_w = width * 0.9
    lines: list[str] = []
    n = 0
    for i, (a, b, _text, ws) in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else None
        end = min(b + 0.9, nxt + 0.12) if nxt is not None else b + 0.9
        end = min(duration, max(end, a + 0.8))
        a = max(0.0, a)
        if end - a < 0.25:
            continue
        top, main = _tiers(ws)
        col = colors[n % len(colors)]
        n += 1
        dur_ms = int((end - a) * 1000)
        out_ms = max(0, dur_ms - 200)

        def words_tags(seq: list[Word], upper: bool = True) -> list[tuple[int, str]]:
            res = []
            for w in seq:
                dt = max(0, min(int((w.start - a) * 1000), max(0, dur_ms - 300)))
                t = _ass_escape(_fix_text(cfg, w.text))
                res.append((dt, t.upper() if upper else t))
            return res

        def fit(seq: list[Word], size: float, spacing: float) -> int:
            """Масштаб строки, чтобы она влезла по ширине (примерная ширина символа ~0.62 кегля)."""
            chars = _text_len(seq)
            est = chars * (size * 0.64 + spacing)
            return 100 if est <= max_w else max(55, int(100 * max_w / est))

        if top:
            sc = fit(top, size1, 6)
            body = "".join(
                f"{{\\alpha&HFF&\\blur6\\t({dt},{dt + 160},\\alpha&H00&\\blur0.6)}}{t} "
                for dt, t in words_tags(top))
            lines.append(f"Dialogue: 1,{_ass_time(a)},{_ass_time(end)},Top,,0,0,0,,"
                         f"{{\\an5\\pos({cx},{y1})\\fscx{sc}\\fscy{sc}\\t({out_ms},{dur_ms},\\alpha&HFF&\\blur8)}}"
                         + body.rstrip())
        sc = fit(main, size2, 3)
        tags = words_tags(main)
        glow = "".join(
            f"{{\\3a&HFF&\\t({dt},{dt + 160},\\3a&H40&)}}{t} " for dt, t in tags).rstrip()
        text = "".join(
            f"{{\\alpha&HFF&\\blur7\\fscx{int(sc * 1.08)}\\t({dt},{dt + 170},\\alpha&H00&\\blur0.8\\fscx{sc})}}{t} "
            for dt, t in tags).rstrip()
        pos = f"\\an5\\pos({cx},{y2})\\fscx{sc}\\fscy{sc}"
        lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(end)},Glow,,0,0,0,,"
                     f"{{{pos}\\3c{col}\\t({out_ms},{dur_ms},\\3a&HFF&)}}{glow}")
        lines.append(f"Dialogue: 2,{_ass_time(a)},{_ass_time(end)},Main,,0,0,0,,"
                     f"{{{pos}\\c{col}\\t({out_ms},{dur_ms},\\alpha&HFF&\\blur8)}}{text}")
    f1 = str(s.get("cine_top_font", "Montserrat SemiBold"))
    f2 = str(s.get("cine_main_font", "Montserrat ExtraBold"))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Top,{f1},{size1},&H00F4F4F4,&H00FFFFFF,&H90000000,&HA0000000,0,0,0,0,100,100,6,0,1,0,{max(1, size1 // 14)},5,20,20,0,204
Style: Glow,{f2},{size2},&HFF000000,&HFF000000,&H00FFFFFF,&HFF000000,0,1,0,0,100,100,3,0,1,{max(6, size2 // 6)},0,5,20,20,0,204
Style: Main,{f2},{size2},&H00FFFFFF,&H00FFFFFF,&HFF000000,&HB0000000,0,1,0,0,100,100,3,0,1,0,{max(1, size2 // 18)},5,20,20,0,204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # свечение = размытая цветная обводка без заливки
    lines = [ln.replace(",Glow,,0,0,0,,{", ",Glow,,0,0,0,,{\\blur" + str(max(8, size2 // 7)) + "\\1a&HFF&", 1)
             for ln in lines]
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return n


def build_ass(cfg: Config, words: list[Word], srt_lines: list[tuple[float, float, str]],
              offset: float, duration: float, width: int, height: int, path: Path,
              margin_v: int | None = None, palette: list[str] | None = None) -> int:
    """Пишет файл .ass с фразами на отрезке [offset, offset+duration]. Возвращает число фраз."""
    s = cfg.subtitles
    # крупный капс (pop) шире обычного текста — фразы короче, чтобы влезать в ширину кадра
    pop_style = str(s.get("style", "")) == "pop"
    # pop: фраза до 4 слов в 1–2 строки (перенос делает libass), обычный стиль — одна короткая строка
    max_chars = int(s.get("pop_max_chars", 24)) if pop_style else int(s.max_chars)
    max_words = int(s.get("pop_max_words", 4)) if pop_style else int(s.max_words)
    # где есть перевод из .srt, распознанную речь не показываем — ни поверх строк, ни в коротких паузах
    # между ними (там обычно звучит тот же закадровый перевод, и слова путаются со строками .srt)
    srt_spans = [(a, b) for a, b, _ in srt_lines]
    words = [w for w in words if not any(a - 1.2 < (w.start + w.end) / 2 < b + 1.2 for a, b in srt_spans)]
    words = _join_hyphens(words)
    events = _chunks(words, max_words, max_chars)
    for a, b, t in srt_lines:
        events += [(x, y, z, None) for x, y, z in _split_line(a, b, re.sub(r"^[-–—]\s*", "", t), max_chars)]
    events.sort(key=lambda ev: ev[0])
    # фраза в самом начале ролика со строчной буквы — хвост реплики, начатой до куска: не показываем
    # (слово началось ещё до начала куска); если фраза началась внутри ролика — это начало, а не хвост,
    # просто Whisper написал её со строчной: делаем заглавную
    while events and events[0][0] - offset < 0.12 and events[0][2][:1].islower():
        events.pop(0)
    if events and events[0][2][:1].islower():
        a, b, t, ws = events[0]
        events[0] = (a, b, t[:1].upper() + t[1:], ws)

    # исправления частых ошибок распознавания (subtitles.fixes в config.yaml: «как услышал» -> «как надо»)
    fixes = (cfg._data.get("subtitles") or {}).get("fixes") or {}

    def fix(t: str) -> str:
        for wrong, right in fixes.items():
            t = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, t)
        return t

    style = str(s.get("style", "karaoke"))
    karaoke = style == "karaoke"
    pop = style == "pop"
    if pop:
        from .looks import ass_color
        colors = [ass_color(c) for c in (palette or ["#35F2FF", "#FFE14D", "#FF4FD8", "#7CFF5B"])]
        color_i = 0
    hl = str(s.get("highlight", "&H004AD5FF"))   # цвет слова, которое звучит (ASS: &HAABBGGRR) — тёплый жёлтый
    lines = []
    for i, (a, b, text, ws) in enumerate(events):
        a, b = a - offset, b - offset
        if i + 1 < len(events):
            b = min(max(b, a + 0.5), events[i + 1][0] - offset)  # без наложений, но не слишком коротко
        a, b = max(0.0, a), min(duration, max(b, a + 0.4))
        if b - a < 0.15:
            continue
        text = fix(text)
        if pop:
            # капсом; точки и запятые в конце фразы не нужны (восклицания и вопросы оставляем)
            tokens = re.sub(r"[.,;:]+$", "", text.upper()).split()
            ws_ok = ws if ws and len(ws) == len(tokens) else None
            for layer, glow in ((0, True), (1, False)):
                body = _pop_line(tokens, ws_ok, a, b, offset, colors, color_i, glow)
                lines.append(f"Dialogue: {layer},{_ass_time(a)},{_ass_time(b)},Sub,,0,0,0,,{body}")
            color_i += len(tokens)
            continue
        # фраза «выпрыгивает»: появляется чуть меньше и за 0.1 с вырастает до нормального размера
        pop = r"{\fscx86\fscy86\t(0,100,\fscx100\fscy100)}" if karaoke else ""
        tokens = text.split()
        if karaoke and ws and len(tokens) == len(ws):
            # караоке: каждое слово подсвечивается, пока звучит
            for k, w in enumerate(ws):
                ka = a if k == 0 else max(a, w.start - offset)
                kb = b if k == len(ws) - 1 else min(b, ws[k + 1].start - offset)
                if kb - ka < 0.04:
                    continue
                body = " ".join(_ass_escape(t) if j != k else
                                "{\\c" + hl + "&}" + _ass_escape(t) + "{\\c&H00FFFFFF&}"
                                for j, t in enumerate(tokens))
                lines.append(f"Dialogue: 0,{_ass_time(ka)},{_ass_time(kb)},Sub,,0,0,0,,{pop if k == 0 else ''}{body}")
        else:
            lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Sub,,0,0,0,,{pop}{_ass_escape(text)}")

    size = int(width * float(s.get("pop_size_pct", 7.2) if pop else s.font_size_pct) / 100)
    if margin_v is None:
        margin_v = int(height * float(s.position_from_bottom_pct) / 100)
    font = str(s.get("pop_font", "Russo One")) if pop else s.font
    outline = max(4, size // 9) if pop else max(3, size // 10)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H78000000,-1,0,0,0,100,100,{2 if pop else 0},0,1,{outline},{max(1, size // 22)},2,50,50,{margin_v},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
