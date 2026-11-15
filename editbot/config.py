from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Обёртка над config.yaml с доступом через точку: cfg.edit.target_duration."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as e:
            raise AttributeError(f"нет параметра конфигурации '{name}'") from e
        return Config(value) if isinstance(value, dict) else value

    def get(self, name: str, default: Any = None) -> Any:
        value = self._data.get(name, default)
        return Config(value) if isinstance(value, dict) else value

    def path(self, name: str) -> Path:
        p = Path(self._data[name])
        return p if p.is_absolute() else ROOT / p


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = Config(yaml.safe_load(f))
    for key in ("library", "music", "output", "work", "logs"):
        cfg.paths.path(key).mkdir(parents=True, exist_ok=True)
    cfg.paths.path("state").parent.mkdir(parents=True, exist_ok=True)
    return cfg


def setup_logging(cfg: Config) -> None:
    log_file = cfg.paths.path("logs") / "editbot.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.FileHandler(log_file, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file)
    for noisy in ("httpx", "httpx2", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
