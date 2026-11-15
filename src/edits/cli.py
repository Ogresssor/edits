from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .core import Store, read, save, sha256


def main():
    parser = argparse.ArgumentParser(description='Edits Studio — local workflow; never publishes automatically')
    parser.add_argument('--workspace', type=Path, default=Path('.local'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    ingest = sub.add_parser('ingest'); ingest.add_argument('source', type=Path)
    analyze = sub.add_parser('analyze'); analyze.add_argument('source', type=Path)
    render = sub.add_parser('render'); render.add_argument('plan', type=Path)
    render.add_argument('--profile', choices=['clean','accent','cinema','intrigue'], default='clean')
    render.add_argument('--preview', action='store_true')
    check = sub.add_parser('check'); check.add_argument('file', type=Path)
    approve = sub.add_parser('approve'); approve.add_argument('sha256')
    approve.add_argument('--reviewer', required=True)
    approve.add_argument('--reviewed-video-and-audio', action='store_true')
    queue = sub.add_parser('queue'); queue.add_argument('sha256'); queue.add_argument('--at', required=True)
    sub.add_parser('status')
    args = parser.parse_args()
    try:
        store = Store(args.workspace)
        if args.command == 'doctor':
            from .media import executable, run
            print(executable()); print(run(['-version']).strip() or 'FFmpeg available')
        elif args.command == 'ingest':
            from .media import probe
            source = args.source.resolve(); digest = store.source_hash(source)
            path = store.root / 'assets' / (digest + '.json')
            if not path.exists():
                save(path, dict(path=str(source), sha256=digest, info=probe(source),
                                full_editorial_analysis=False, scenes=[], captions=[]))
            print(path)
        elif args.command == 'analyze':
            from .analysis import analyze
            print(analyze(args.source, store))
        elif args.command == 'render':
            from .render import render
            print(render(args.plan, store, args.profile, args.preview))
        elif args.command == 'check':
            from .render import check
            report = check(args.file.resolve(), store)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['passed'] else 2
        elif args.command == 'approve':
            store.approve(args.sha256, args.reviewer, args.reviewed_video_and_audio)
            print('Approved exact SHA-256; no upload performed')
        elif args.command == 'queue':
            store.enqueue(args.sha256, datetime.fromisoformat(args.at.replace('Z','+00:00')))
            print('Added to local publication plan; no upload performed')
        elif args.command == 'status':
            print(json.dumps(store.queue(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
