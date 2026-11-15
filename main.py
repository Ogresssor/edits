"""Точка входа.

    python main.py run                 — один цикл: трендовый фильм -> эдиты -> YouTube
    python main.py run --movie FILE    — сделать эдиты из конкретного файла
    python main.py run --no-upload     — только смонтировать, не выкладывать
    python main.py daemon              — автономный режим по расписанию
    python main.py auth                — один раз авторизовать YouTube-аккаунт
    python main.py trends              — показать тренды и какие из них есть в библиотеке
    python main.py upload              — догрузить смонтированные, но не выложенные ролики
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from editbot.config import load_config, setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Автономная фабрика эдитов по фильмам")
    parser.add_argument("--config", help="путь к config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="один цикл")
    run.add_argument("--movie", type=Path, help="конкретный видеофайл")
    run.add_argument("--title", help="название фильма для --movie")
    run.add_argument("--no-upload", action="store_true", help="не загружать на YouTube")
    run.add_argument("--count", type=int, help="сколько эдитов сделать (по умолчанию curation.edits_per_movie)")
    run.add_argument("--fresh", action="store_true", help="выбрать сцены заново (не брать из кэша)")
    run.add_argument("--resume", action="store_true", help="продолжить прерванный запуск: готовые ролики не монтировать")
    run.add_argument("--redo", action="store_true", help="переделать отобранные сцены в текущем формате "
                                                         "(кроме отклонённых и уже вышедших)")
    d = sub.add_parser("daemon", help="автономный режим")
    d.add_argument("--no-upload", action="store_true")
    sub.add_parser("auth", help="авторизация YouTube")
    b = sub.add_parser("batch", help="сериал/папка: монтировать серии по порядку и выкладывать по расписанию")
    b.add_argument("--dir", type=Path, required=True, help="папка с сериями (подпапки тоже)")
    b.add_argument("--title", help="название (например, «Менталист»)")
    b.add_argument("--count", type=int, help="эдитов из серии")
    b.add_argument("--keep-ahead", type=int, default=6, help="держать столько роликов в расписании наперёд")
    b.add_argument("--limit", type=int, default=3, help="не больше стольких серий за запуск")
    b.add_argument("--no-upload", action="store_true")
    sub.add_parser("trends", help="тренды TMDB и ваша библиотека")
    up = sub.add_parser("upload", help="догрузить невыложенные ролики")
    up.add_argument("--only", help="загрузить только ролики, путь к которым содержит этот текст "
                                   "(например, папку фильма: --only Токийский)")
    cl = sub.add_parser("clean", help="освободить диск: кэш музыки, старые ролики, временные файлы")
    cl.add_argument("--dry-run", action="store_true", help="только показать, что будет удалено")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)
    log = logging.getLogger("main")

    from editbot.orchestrator import Orchestrator

    if args.cmd == "auth":
        from editbot.agents.uploader import UploaderAgent

        UploaderAgent(cfg).authorize()
        return 0

    orch = Orchestrator(cfg, upload=not getattr(args, "no_upload", False))
    if args.cmd == "batch":
        orch.batch(args.dir, args.title, args.count, args.keep_ahead, args.limit)
        return 0
    if args.cmd == "trends":
        for t, path in orch.trends.report()[:40]:
            mark = f"  ✔ {path.name}" if path else ""
            print(f"#{t.rank:<3} {t.title} ({t.year}) — popularity {t.popularity:.0f}{mark}")
        return 0
    if args.cmd == "upload":
        if not orch.upload_enabled:
            log.error("Загрузка на YouTube выключена (youtube.enabled в config.yaml)")
            return 1
        orch.retry_pending(only=args.only)
        return 0
    if args.cmd == "clean":
        from editbot.cleanup import Cleaner

        Cleaner(cfg, orch.state, dry_run=args.dry_run).all()
        return 0
    if args.cmd == "daemon":
        try:
            orch.daemon()
        except KeyboardInterrupt:
            log.info("Остановлено")
        return 0

    if args.movie and not args.movie.exists():
        log.error("Файл не найден: %s", args.movie)
        return 1
    orch.redo = args.redo
    videos = orch.run_cycle(args.movie, args.title, count=args.count, fresh=args.fresh, resume=args.resume)
    for v in videos:
        print(v)
    return 0 if videos else 1


if __name__ == "__main__":
    sys.exit(main())
