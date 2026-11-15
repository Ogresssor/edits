"""Агент-куратор: Claude читает фильм целиком (карта сюжета с репликами) и смотрит кадры, а затем
придумывает ролики. Два вида:

  * story   — мини-история до ~30 с с одной законченной мыслью: завязка -> событие -> развязка/панчлайн.
              Собирается из 1–3 кусков одной сюжетной линии (лишнее между ними вырезается);
  * montage — общий эдит по фильму: нарезка пиковых моментов под бит.

Решение сохраняется в work/<фильм>/curation.json: повторный монтаж не платит за Claude ещё раз,
а файл можно поправить вручную. Без ANTHROPIC_API_KEY работает эвристика по сигналам.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from .. import ffmpeg
from ..config import Config, env
from .analyzer import Analysis, Moment
from .research import Brief
from .trends import Movie

log = logging.getLogger("curator")

CURATION_VERSION = 4


@dataclass
class Fragment:
    start: float
    end: float
    voice: bool = False   # звучит реплика из фильма (с субтитрами, музыка приглушается); иначе — экшен под музыку
    fx: str = ""          # "" | "slowmo" | "ramp" (резкое замедление посреди движения) | "fast"
    score: float = 0.0    # важность реплики (линия: при нехватке времени первой убирается наименее важная)


@dataclass
class EditPlan:
    kind: str                      # "story" | "montage"
    theme: str
    hook_text: str
    music_query: str = ""
    use_music: bool = True
    thought: str = ""              # законченная мысль ролика одним предложением
    segments: list[tuple[float, float]] = field(default_factory=list)   # куски фильма (story)
    moments: list[Moment] = field(default_factory=list)                 # моменты (montage)
    fragments: list[Fragment] = field(default_factory=list)             # фрагменты (edit)
    finale: list[float] = field(default_factory=list)                   # кадры финальной нарезки (scene)
    look: object = None                                                 # образ (editbot/looks.py)
    body_len: float = 0.0                                               # длина основной части (scene)
    mood: str = ""                                                      # настроение (arc): tension / intrigue / …


CURATOR_SYSTEM = """Ты — монтажёр YouTube Shorts / TikTok канала с ЭДИТАМИ по фильмам.

Эдит — это не нарезка сцены, а ритмичный клип под музыку 15–30 секунд со своей темой и законченной мыслью:
- фрагменты из фильма по 1–5 секунд (не длиннее!), кадр постоянно меняется, склейки ложатся в бит музыки;
- мысль рассказывают 1–3 ключевые реплики героев (voice=true: фраза звучит из фильма, на экране субтитры) —
  короткие, до 5 секунд, законченная фраза без обрыва; между ними — экшен и эмоции под музыку (voice=false):
  взгляды, движения, машины, удары, реакции, красивые планы — то, что усиливает мысль реплик;
- драматургия: сильный первый кадр -> реплика-завязка -> нарастание -> кульминация (самый мощный кадр,
  часто с замедлением) -> реплика-панчлайн или финальный кадр, который ставит точку;
- fx для экшен-фрагментов: "ramp" — резкое замедление посреди движения (заносы, прыжки, удары), "slowmo" —
  красивое замедление (взгляд, волосы, искры), "" — обычная скорость; у реплик fx всегда "".

Ты получаешь карту сюжета: весь фильм блоками по 10 секунд — время, накал (⚡), частые склейки (✂) и реплики,
и справку о фильме от исследователя (сюжет, персонажи, известные цитаты; цитаты в дубляже звучат иначе).
Фрагменты эдита можно брать из разных мест фильма, но мысль должна быть одна и понятна без контекста.
Время пиши как в карте: «0:11:17.5». Каждый фрагмент — start/end, длина 1–5 с.

Не используй финал фильма и главные повороты (спойлеры), титры, тёмные и смазанные кадры.
hook_text — короткая спокойная подпись-контекст (2–5 слов, до 32 символов, без восклицаний, капса, кликбейта
и спойлеров), например «Хан о выборе». music_query — 1–3 английских слова (стиль музыки: «phonk», «hip hop beat»,
«dark epic», «emotional»).

Кроме того, оцени пиковые моменты (score 0–10, slowmo для красивых движений, skip для титров, текста, тёмных
кадров) и собери из них один общий эдит: порядок показа, hook_text, music_query."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "edits": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string"},
                    "thought": {"type": "string"},
                    "hook_text": {"type": "string"},
                    "music_query": {"type": "string"},
                    "fragments": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"start": {"type": "string"}, "end": {"type": "string"},
                                       "voice": {"type": "boolean"},
                                       "fx": {"type": "string", "enum": ["", "slowmo", "ramp", "fast"]}},
                        "required": ["start", "end", "voice", "fx"], "additionalProperties": False}},
                },
                "required": ["theme", "thought", "hook_text", "music_query", "fragments"],
                "additionalProperties": False}},
            "moments": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"}, "score": {"type": "number"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "slowmo": {"type": "boolean"}, "skip": {"type": "boolean"},
                },
                "required": ["id", "score", "tags", "slowmo", "skip"], "additionalProperties": False}},
            "montage": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string"},
                    "hook_text": {"type": "string"},
                    "music_query": {"type": "string"},
                    "moment_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["theme", "hook_text", "music_query", "moment_ids"],
                "additionalProperties": False},
        },
        "required": ["edits", "moments", "montage"],
        "additionalProperties": False,
    }


def parse_time(value) -> float:
    """«1:02:03.5», «02:03» или число секунд -> секунды."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", str(value))]
    t = 0.0
    for p in parts[-3:]:
        t = t * 60 + p
    return t


def _image_block(path: str) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


class CuratorAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.c = cfg.curation
        self.client = anthropic.Anthropic() if (self.c.use_claude and env("ANTHROPIC_API_KEY")) else None

    def curate(self, movie: Movie, an: Analysis, brief: Brief | None = None) -> list[EditPlan]:
        cache = an.workdir / "curation.json"
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("version") == CURATION_VERSION:
                    plans = self._apply(data, an, brief)
                    log.info("Решение куратора взято из %s (%d роликов)", cache.name, len(plans))
                    return plans
            except Exception as e:
                log.warning("Не удалось прочитать %s (%s), курирую заново", cache.name, e)
        if self.client:
            try:
                data = self._ask_claude(movie, an, brief)
                plans = self._apply(data, an, brief)
                cache.write_text(json.dumps({"version": CURATION_VERSION, **data}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
                return plans
            except Exception as e:  # сеть, квоты, отказ модели — продолжаем на эвристике
                log.warning("Куратор Claude недоступен (%s), перехожу на эвристику", e)
        else:
            log.info("ANTHROPIC_API_KEY не задан — подбор сцен эвристикой (без понимания сюжета)")
        return self._heuristic(an)

    # ---------- эдиты-сцены (основной формат) ----------
    SCENES_VERSION = 5

    SCENES_SYSTEM = """Ты — монтажёр канала с эдитами по фильмам для Shorts/TikTok. Формат эдита: ОДНА цельная сцена
фильма 14–26 секунд (диалог с законченной мыслью: завязка -> реплики -> панчлайн/точка), потом короткая нарезка
лучших кадров этой же сцены под бит. Тебе дают карту сюжета (весь фильм блоками по 10 с с репликами) и справку
о фильме. Выбери самые яркие, смешные, напряжённые или культовые сцены, понятные без контекста.
Сцена начинается и заканчивается на паузе между репликами (не посреди фразы), внутри нет долгого молчания.
Не бери финал фильма и главные повороты (спойлеры), титры. title — спокойный заголовок-плашка 2–6 слов
(о чём сцена, без кликбейта и спойлеров), например «Получает чаевые за красивую улыбку».
Время — как в карте: «0:11:17.5»."""

    SCENES_SCHEMA = {
        "type": "object",
        "properties": {"scenes": {"type": "array", "items": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}, "title": {"type": "string"},
                           "thought": {"type": "string"}},
            "required": ["start", "end", "title", "thought"], "additionalProperties": False}}},
        "required": ["scenes"], "additionalProperties": False,
    }

    def curate_scenes(self, movie: Movie, an: Analysis, brief: Brief | None, count: int,
                      fresh: bool = False) -> list[EditPlan]:
        """count эдитов-сцен: из кэша (work/<фильм>/scenes.json — можно править вручную), Claude или автоподбор."""
        from .. import subtitles
        from ..scenes import ScenePicker

        cache = an.workdir / "scenes.json"
        scenes: list[dict] = []
        if cache.exists() and not fresh:
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("version") == self.SCENES_VERSION and len(data["scenes"]) >= count:
                    scenes = data["scenes"]
                    log.info("Сцены взяты из %s (%d)", cache.name, len(scenes))
            except Exception as e:
                log.warning("Не удалось прочитать %s (%s), выбираю заново", cache.name, e)
        picker = ScenePicker()
        sig = dict(__import__("numpy").load(an.workdir / "signals.npz"))
        if not scenes and self.client:
            try:
                scenes = self._claude_scenes(movie, an, brief, count)
                for sc in scenes:
                    a, b = parse_time(sc["start"]), parse_time(sc["end"])
                    sc.update(start=a, end=b, finale=picker.finale_shots(sig, a, b))
            except Exception as e:
                log.warning("Куратор Claude недоступен (%s) — автоподбор сцен", e)
                scenes = []
        if not scenes:
            picked = picker.pick(an.workdir, an.info.duration, count,
                                 brief.spoiler_after if brief else 0.0,
                                 brief.quotes if brief else [], brief.names if brief else [])
            scenes = []
            for c in picked:
                # заголовок — по точной расшифровке (medium), а не по черновой
                words = subtitles.transcribe(self.cfg, movie.path, max(0.0, c.start - 1.0), c.end - c.start + 2,
                                             an.workdir, an.info.audio_index)
                text = " ".join(w.text for w in words if c.start - 0.2 <= w.start <= c.end)
                fixes = (self.cfg._data.get("subtitles") or {}).get("fixes") or {}
                for wrong, right in fixes.items():
                    text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text)
                title = picker.title(text or c.text, brief.names if brief else [])
                scenes.append({"start": round(c.start, 2), "end": round(c.end, 2), "title": title,
                               "thought": text[:160], "finale": [round(x, 2) for x in c.finale]})
        cache.write_text(json.dumps({"version": self.SCENES_VERSION, "scenes": scenes}, ensure_ascii=False,
                                    indent=1), encoding="utf-8")
        plans = []
        for sc in scenes[:count]:
            plans.append(EditPlan("scene", sc["title"], sc["title"], "", True, sc.get("thought", ""),
                                  segments=[(float(sc["start"]), float(sc["end"]))],
                                  finale=[float(x) for x in sc.get("finale", [])]))
        return plans

    # ---------- эдиты-линии (формат эталонных каналов) ----------
    ARCS_VERSION = 11     # 11: цельная сцена (arc.scene); 10: ролик начинается с реплики-крючка (hook_min); 9: короткий формат

    def curate_arcs(self, movie: Movie, an: Analysis, brief: Brief | None, count: int,
                    fresh: bool = False) -> list[EditPlan]:
        """count сюжетных линий: из кэша work/<фильм>/arcs.json (можно править вручную: реплики, кадры,
        заголовок) или автоподбор (editbot/arcs.py)."""
        from .. import subtitles
        from ..arcs import ArcPicker

        cache = an.workdir / "arcs.json"
        arcs: list[dict] = []
        used: list[list[float]] = []      # отрезки фильма, уже взятые в ролики (в т.ч. прошлых версий) — не повторяем
        if cache.exists():
            try:
                old = json.loads(cache.read_text(encoding="utf-8"))
                used = old.get("used", []) + [[ln[0], ln[1]] for x in old.get("arcs", []) for ln in x["lines"]]
            except Exception:
                pass
        if cache.exists() and not fresh:
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("version") == self.ARCS_VERSION and len(data["arcs"]) >= count:
                    arcs = data["arcs"]
                    log.info("Сюжетные линии взяты из %s (%d)", cache.name, len(arcs))
            except Exception as e:
                log.warning("Не удалось прочитать %s (%s), выбираю заново", cache.name, e)
        if not arcs:
            a = dict(self.cfg._data.get("arc") or {})
            picker = ArcPicker(float(a.get("window_sec", 180)), int(a.get("lines", 5)), int(a.get("min_lines", 3)),
                               budget=float(a.get("speech_sec", float(a.get("story_sec", 45)) - 14)),
                               line_max=float(a.get("line_max_sec", 10)), hook_min=float(a.get("hook_min", 0.0)),
                               scene=bool(a.get("scene", False)))
            tr = lambda s, d: subtitles.transcribe(self.cfg, movie.path, s, d, an.workdir, an.info.audio_index)
            picked = picker.pick(an.workdir, an.info.duration, count, tr,
                                 brief.spoiler_after if brief else 0.0,
                                 brief.quotes if brief else [], brief.names if brief else [],
                                 brief.genres if brief else [],
                                 avoid=[(float(x[0]), float(x[1])) for x in used])
            # текст реплик (и плашка из него) — по точной расшифровке каждой реплики, как в субтитрах:
            # в расшифровке всего фильма бывают ошибки («На бутерброду вздвинулся?»)
            for c in picked:
                exact = []
                for a_, b_, t, sc in c.lines:
                    ws = [w for w in tr(max(0.0, a_ - 1.0), b_ - a_ + 2.0) if a_ - 0.4 <= w.start <= b_ + 0.2]
                    exact.append((a_, b_, " ".join(w.text.strip() for w in ws) or t, sc))
                c.lines = exact
                c.title = ArcPicker.title(c.lines)
            fix = lambda t: subtitles.fix_text(self.cfg, t)
            arcs = [{"title": fix(c.title), "start": round(c.start, 1), "end": round(c.end, 1),
                     "lines": [[a_, b_, fix(t), sc] for a_, b_, t, sc in c.lines],
                     "broll": [round(x, 2) for x in c.broll], "finale": [round(x, 2) for x in c.finale],
                     "mood": c.mood}
                    for c in picked]
            used_old = [u for u in used if not any(u == [ln[0], ln[1]] for x in arcs for ln in x["lines"])]
            cache.write_text(json.dumps({"version": self.ARCS_VERSION, "arcs": arcs, "used": used_old},
                                        ensure_ascii=False,
                                        indent=1), encoding="utf-8")
        plans = []
        for arc in arcs[:count]:
            frags = [Fragment(float(ln[0]), float(ln[1]), True, score=float(ln[3]) if len(ln) > 3 else 0.0)
                     for ln in arc["lines"]]
            frags += [Fragment(float(t), float(t) + 2.0, False) for t in arc.get("broll", [])]
            plans.append(EditPlan("arc", arc["title"], arc["title"], "", True,
                                  " / ".join(ln[2] for ln in arc["lines"]), fragments=frags,
                                  finale=[float(x) for x in arc.get("finale", [])],
                                  mood=arc.get("mood", "")))
        return plans

    def _claude_scenes(self, movie: Movie, an: Analysis, brief: Brief | None, count: int) -> list[dict]:
        content = [{"type": "text", "text": f"Фильм: «{movie.title}» {movie.year}"},
                   *([{"type": "text", "text": brief.for_curator()}] if brief else []),
                   {"type": "text", "text": an.story.read_text(encoding="utf-8")},
                   {"type": "text", "text": f"Выбери {count} лучших сцен для эдитов (от лучшей). Ответ строго по схеме."}]
        log.info("Куратор Claude выбирает %d сцен…", count)
        with self.client.beta.messages.stream(
            model=self.c.model, max_tokens=8000, system=self.SCENES_SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self.c.effort, "format": {"type": "json_schema", "schema": self.SCENES_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            r = stream.get_final_message()
        if r.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"stop_reason={r.stop_reason}")
        return json.loads(next(b.text for b in r.content if b.type == "text"))["scenes"]

    # ---------- Claude ----------
    def _ask_claude(self, movie: Movie, an: Analysis, brief: Brief | None = None) -> dict:
        n = int(self.c.stories_per_movie)
        content: list[dict] = [
            {"type": "text", "text": f"Фильм: «{movie.title}» {movie.year}\nОписание: {movie.overview or '—'}"},
            *([{"type": "text", "text": brief.for_curator()}] if brief else []),
            {"type": "text", "text": an.story.read_text(encoding="utf-8")},
            {"type": "text", "text": f"Кадры {len(an.scenes)} сцен-кандидатов (◆ в карте), по 4 кадра каждой:"},
        ]
        for sc in an.scenes:
            content.append({"type": "text", "text": f"Сцена-кандидат #{sc.id}: "
                                                    f"{ffmpeg.fmt_time(sc.start)}–{ffmpeg.fmt_time(sc.end)}"})
            content.append(_image_block(sc.preview))
        content.append({"type": "text", "text": f"{len(an.moments)} пиковых моментов для общего эдита:"})
        for m in an.moments:
            content.append({"type": "text", "text": f"Момент #{m.id} — {ffmpeg.fmt_time(m.peak)}"})
            content.append(_image_block(m.keyframe))
        content.append({"type": "text", "text": (
            f"Придумай {n} эдитов (от лучшего к худшему) — каждый со своей темой и законченной мыслью, "
            "8–16 фрагментов по 1–5 с — "
            f"и общий эдит из {int(self.c.moments_per_edit) - 4}–{int(self.c.moments_per_edit)} моментов. "
            "Фрагменты можно брать из любого места карты. Ответ строго по схеме.")})

        log.info("Куратор Claude читает карту сюжета и смотрит %d сцен, %d моментов…",
                 len(an.scenes), len(an.moments))
        with self.client.beta.messages.stream(
            model=self.c.model,
            max_tokens=32000,
            system=CURATOR_SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self.c.effort, "format": {"type": "json_schema", "schema": _schema()}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RuntimeError("модель отказалась обрабатывать запрос")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("ответ обрезан по max_tokens")
        u = response.usage
        log.info("Куратор ответил (токены: вход %d, выход %d)", u.input_tokens, u.output_tokens)
        return json.loads(next(b.text for b in response.content if b.type == "text"))

    def _apply(self, data: dict, an: Analysis, brief: Brief | None = None) -> list[EditPlan]:
        moments = {m.id: m for m in an.moments}
        for r in data.get("moments", []):
            m = moments.get(r["id"])
            if m:
                m.ai_score = -1.0 if r["skip"] else float(r["score"])
                m.tags = r["tags"]
                m.slowmo = bool(r["slowmo"])

        plans: list[EditPlan] = []
        spoiler = brief.spoiler_after if brief and brief.spoiler_after else an.info.duration
        for e in data.get("edits", [])[: int(self.c.stories_per_movie)]:
            frs = [Fragment(parse_time(f["start"]), parse_time(f["end"]), bool(f.get("voice")), f.get("fx") or "")
                   for f in e["fragments"]]
            frs = [f for f in frs if 0.3 <= f.end - f.start <= 9 and f.end <= an.info.duration]
            if any(f.end > spoiler + 1 for f in frs):
                log.info("Эдит «%s» заходит в финал фильма (спойлер) — пропускаю", e["theme"])
                continue
            if len(frs) >= 3:
                plans.append(EditPlan("edit", e["theme"], e["hook_text"].strip()[:60], e.get("music_query", ""),
                                      True, e.get("thought", ""), fragments=frs))
        for e in data.get("stories", [])[: max(0, int(self.c.stories_per_movie) - len(plans))]:
            segs = sorted((parse_time(s["start"]), parse_time(s["end"])) for s in e["segments"])
            segs = [(a, b) for a, b in segs if b - a >= 1.0 and b <= an.info.duration]
            if brief and brief.spoiler_after and segs and segs[-1][1] > brief.spoiler_after + 1:
                log.info("История «%s» заходит в финал фильма (спойлер) — пропускаю", e["theme"])
                continue
            if segs:
                plans.append(EditPlan("story", e["theme"], e["hook_text"].strip()[:60], e.get("music_query", ""),
                                      bool(e.get("use_music", True)), e.get("thought", ""), segments=segs))
        mt = data.get("montage")
        if self.c.montage and mt:
            chosen = [moments[i] for i in mt["moment_ids"] if i in moments and (moments[i].ai_score or 0) >= 0]
            if len(chosen) >= 4:
                plans.append(EditPlan("montage", mt["theme"], mt["hook_text"].strip()[:60],
                                      mt.get("music_query", ""), True, moments=chosen))
        if not plans:
            raise RuntimeError("в решении куратора нет ни одного пригодного ролика")
        return plans

    # ---------- эвристика ----------
    def _heuristic(self, an: Analysis) -> list[EditPlan]:
        plans = []
        best = sorted(an.scenes, key=lambda sc: -sc.score)[: int(self.c.stories_per_movie)]
        hooks = ["Сцена из фильма", "Один момент", "Эпизод"]
        for i, sc in enumerate(sorted(best, key=lambda s: s.start)):
            plans.append(EditPlan("story", f"Сцена {i + 1}", hooks[i % len(hooks)], "dark epic", True,
                                  segments=[(sc.start, sc.end)]))
        if self.c.montage and len(an.moments) >= 4:
            picked = sorted(an.moments, key=lambda m: -m.score)[: int(self.c.moments_per_edit)]
            body = sorted(picked[2:], key=lambda m: m.score)
            order = [picked[0], *body, picked[1]] if len(picked) > 2 else picked
            plans.append(EditPlan("montage", "Лучшие моменты фильма", "Лучшие моменты",
                                  "epic trailer", True, moments=order))
        return plans
