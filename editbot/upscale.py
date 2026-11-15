"""Нейросетевое увеличение разрешения (Real-ESRGAN general-x4v3) на видеокарте через PyTorch.

Исходники фильмов часто 720×400: окно 9:16 из такого кадра — всего ~224×400 пикселей, а растянуть его нужно
до 1080×1920. Обычное растяжение даёт «мыло»; Real-ESRGAN дорисовывает чёткие края и текстуры.

Архитектура SRVGGNetCompact (как в github.com/xinntao/Real-ESRGAN) описана здесь же, веса скачиваются
с официального релиза один раз в models/. Если PyTorch/CUDA недоступны — вернётся None и монтаж
продолжится с обычным (lanczos) растяжением.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np

log = logging.getLogger("upscale")

WEIGHTS = {
    "general": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
    # та же сеть, обученная сильнее убирать шум/артефакты сжатия; смешиваем с основной (denoise)
    "wdn": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-wdn-x4v3.pth",
}


def _download(name: str):
    import requests

    from .config import ROOT

    path = ROOT / "models" / WEIGHTS[name].rsplit("/", 1)[1]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Скачиваю веса Real-ESRGAN %s (~5 МБ)…", path.name)
        r = requests.get(WEIGHTS[name], timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def _net():
    import torch
    from torch import nn
    from torch.nn import functional as F

    class SRVGGNetCompact(nn.Module):
        def __init__(self, num_feat: int = 64, num_conv: int = 32, upscale: int = 4):
            super().__init__()
            self.upscale = upscale
            body: list[nn.Module] = [nn.Conv2d(3, num_feat, 3, 1, 1), nn.PReLU(num_parameters=num_feat)]
            for _ in range(num_conv):
                body += [nn.Conv2d(num_feat, num_feat, 3, 1, 1), nn.PReLU(num_parameters=num_feat)]
            body.append(nn.Conv2d(num_feat, 3 * upscale * upscale, 3, 1, 1))
            self.body = nn.ModuleList(body)
            self.upsampler = nn.PixelShuffle(upscale)

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            out = x
            for layer in self.body:
                out = layer(out)
            return self.upsampler(out) + F.interpolate(x, scale_factor=self.upscale, mode="nearest")

    return SRVGGNetCompact()


class Upscaler:
    """x4: uint8 RGB (N, H, W, 3) -> uint8 RGB (N, 4H, 4W, 3)."""

    def __init__(self, denoise: float = 0.5):
        import torch

        def load(name: str) -> dict:
            sd = torch.load(_download(name), map_location="cpu", weights_only=True)
            return sd.get("params_ema") or sd.get("params") or sd

        a = load("general")
        if denoise > 0:  # интерполяция весов, как --denoise_strength в официальном скрипте
            b = load("wdn")
            a = {k: (1 - denoise) * a[k] + denoise * b[k] for k in a}
        net = _net()
        net.load_state_dict(a, strict=True)
        self.dev = torch.device("cuda")
        self.net = net.eval().half().to(self.dev)
        self.torch = torch

    def __call__(self, frames: np.ndarray, batch: int = 8) -> np.ndarray:
        torch = self.torch
        out = []
        with torch.inference_mode():
            for i in range(0, len(frames), batch):
                x = torch.from_numpy(frames[i:i + batch]).to(self.dev).permute(0, 3, 1, 2).half().div_(255)
                y = self.net(x).clamp_(0, 1).mul_(255).round_().byte().permute(0, 2, 3, 1)
                out.append(y.cpu().numpy())
        return np.concatenate(out)


@lru_cache(maxsize=1)
def get(denoise: float = 0.5) -> Upscaler | None:
    try:
        import torch

        if not torch.cuda.is_available():
            log.info("Видеокарта CUDA не найдена — увеличение кадра обычное (lanczos)")
            return None
        up = Upscaler(denoise)
        log.info("Нейросетевое увеличение Real-ESRGAN включено (%s)", torch.cuda.get_device_name(0))
        return up
    except ImportError:
        log.info("PyTorch не установлен — увеличение кадра обычное (lanczos)")
    except Exception as e:
        log.warning("Real-ESRGAN недоступен (%s) — увеличение кадра обычное (lanczos)", e)
    return None
