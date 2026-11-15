"""Агент метаданных: цепляющее название, описание и теги для YouTube."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import anthropic

from ..config import Config, env
from .curator import EditPlan
from .research import Brief
from .trends import Movie

log = logging.getLogger("metadata")


@dataclass
class VideoMeta:
    title: str
    description: str
    tags: list[str]


SYSTEM = """Ты — SMM-специалист YouTube-канала с эдитами по фильмам. Пишешь по-русски.
Название: до 70 символов, спокойно и по делу (о чём момент), содержит название фильма; без кликбейта, капса,
восклицаний и спойлеров концовки, без эмодзи. Описание: 2–4 коротких предложения о настроении эдита и фильме, призыв подписаться,
затем 5–8 хэштегов (обязательно #shorts для вертикального формата, #фильм, хэштег с названием фильма).
Теги: 10–15 поисковых фраз по-русски и по-английски (название фильма, актёры, если знаешь, «эдит», «edit»)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "tags"],
    "additionalProperties": False,
}


class MetadataAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic() if (cfg.curation.use_claude and env("ANTHROPIC_API_KEY")) else None

    def generate(self, movie: Movie, plan: EditPlan, credit: str = "", brief: Brief | None = None) -> VideoMeta:
        meta = None
        if self.client:
            try:
                meta = self._claude(movie, plan, brief)
            except Exception as e:
                log.warning("Метаданные через Claude не получились (%s), использую шаблон", e)
        meta = meta or self._template(movie, plan, brief)
        if credit:  # условие лицензии CC BY — указание автора музыки
            meta.description = f"{meta.description.rstrip()}\n\n{credit}"
        return self._sanitize(meta)

    def _claude(self, movie: Movie, plan: EditPlan, brief: Brief | None = None) -> VideoMeta:
        tags = sorted({t for m in plan.moments for t in (m.tags or [])})
        vertical = self.cfg.edit.format == "vertical"
        if plan.kind in ("story", "edit"):
            what = f"Ролик — мини-история из фильма. Её мысль: {plan.thought or '—'}"
        else:
            what = f"Ролик — общий эдит-нарезка лучших моментов. Настроение: {', '.join(tags) or '—'}"
        prompt = (
            f"Фильм: «{movie.title}» ({movie.year}). Оригинальное название: "
            f"{movie.extra.get('original_title') or '—'}.\nОписание фильма: {movie.overview or '—'}\n"
            f"Тема: {plan.theme}. Текст на экране: «{plan.hook_text}». {what}\n"
            + (f"Актёры: {', '.join(a for a, _ in brief.cast[:6]) or '—'}. Хэштеги от исследователя: "
               f"{' '.join(brief.hashtags)}\n" if brief else "")
            + f"Формат: {'вертикальный Shorts' if vertical else 'горизонтальное видео'}.\n"
            "Сгенерируй название, описание и теги."
        )
        r = self.client.beta.messages.create(
            model=self.cfg.curation.model,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if r.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"stop_reason={r.stop_reason}")
        data = json.loads(next(b.text for b in r.content if b.type == "text"))
        return VideoMeta(data["title"], data["description"], data["tags"])

    def _template(self, movie: Movie, plan: EditPlan, brief: Brief | None = None) -> VideoMeta:
        tag = "#" + "".join(w.capitalize() for w in re.findall(r"\w+", movie.title))
        shorts = " #shorts" if self.cfg.edit.format == "vertical" else ""
        extra = " ".join(h for h in (brief.hashtags if brief else []) if h.lower() != tag.lower())
        cast = [a for a, _ in (brief.cast[:4] if brief else [])]
        orig = (brief.original_title if brief else "") or movie.extra.get("original_title", "")
        about = (f"{plan.thought} Фильм «{movie.title}» {movie.year}." if plan.kind in ("story", "edit") and plan.thought
                 else f"Момент из фильма «{movie.title}» {movie.year}." if plan.kind in ("story", "edit")
                 else f"Лучшие моменты фильма «{movie.title}» {movie.year} в одном эдите.")
        handle = f" {self.cfg.youtube.get('handle')}" if self.cfg.youtube.get("handle") else ""
        return VideoMeta(
            title=f"{plan.theme} | {movie.title}" if plan.kind in ("story", "edit") else f"{movie.title} — {plan.theme[:1].lower() + plan.theme[1:]}",
            description=(f"{about}\n"
                         f"Подписывайся, чтобы не пропустить новые эдиты!{handle}\n\n"
                         f"{tag} {extra} #фильм #эдит #edit #кино{shorts}".replace("  ", " ")),
            tags=[movie.title, orig, f"{movie.title} эдит", *cast, "эдит", "edit",
                  "movie edit", "фильм", "кино", "лучшие моменты"],
        )

    @staticmethod
    def _sanitize(meta: VideoMeta) -> VideoMeta:
        # YouTube запрещает < > в названии/описании; лимиты: название 100, описание 5000, теги 500 символов
        clean = lambda s: s.replace("<", "").replace(">", "").strip()
        title = clean(meta.title)[:100]
        description = clean(meta.description)[:4900]
        tags, total = [], 0
        for t in meta.tags:
            t = clean(t).replace(",", " ")
            if not t or t in tags:
                continue
            cost = len(t) + (2 if " " in t else 0) + 1
            if total + cost > 480:
                break
            tags.append(t)
            total += cost
        return VideoMeta(title, description, tags)
