"""Чтение строк из проекта After Effects (.aep — бинарный RIFX): имена слоёв, композиций и т. п.

Нужно, чтобы получить результат скрипта, когда скриптам After Effects запрещено писать файлы
(настройка «Разрешить скриптам записывать файлы и осуществлять доступ к сети» выключена): скрипт пишет
ответ в имена слоёв, сохраняет проект, а мы читаем эти имена отсюда.
"""
from __future__ import annotations

import struct
from pathlib import Path


def strings(path: Path) -> list[str]:
    """Все строки из чанков «Utf8» проекта (по порядку)."""
    b = Path(path).read_bytes()
    out, i = [], 0
    while True:
        i = b.find(b"Utf8", i)
        if i < 0 or i + 8 > len(b):
            break
        n = struct.unpack(">I", b[i + 4:i + 8])[0]
        if 0 < n < 1_000_000 and i + 8 + n <= len(b):
            out.append(b[i + 8:i + 8 + n].decode("utf-8", "replace"))
            i += 8 + n
        else:
            i += 4
    return out
