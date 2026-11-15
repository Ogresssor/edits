"""Агент загрузки: YouTube Data API v3 (OAuth на ваш аккаунт), отложенная публикация.

Загрузка — по протоколу «resumable upload» через requests: кусками по 8 МБ, с докачкой после обрыва.
Если в Windows включён системный прокси (VPN-клиент вроде Happ / v2rayN), загрузка идёт через него —
без этого соединение с серверами YouTube может обрываться (youtube.proxy в config.yaml: auto / адрес / none).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from .metadata import VideoMeta

log = logging.getLogger("uploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024


class QuotaExceeded(RuntimeError):
    pass


def system_proxy() -> str | None:
    """Системный прокси Windows (Параметры → Сеть → Прокси), если включён."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if not winreg.QueryValueEx(k, "ProxyEnable")[0]:
                return None
            server = str(winreg.QueryValueEx(k, "ProxyServer")[0]).strip()
    except OSError:
        return None
    if not server:
        return None
    if "=" in server:                       # «http=host:port;https=host:port»
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = parts.get("https") or parts.get("http") or next(iter(parts.values()))
    return server if "://" in server else f"http://{server}"


class UploaderAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.y = cfg.youtube
        self._session = None
        setting = str(self.y.get("proxy", "auto") or "auto")
        proxy = system_proxy() if setting == "auto" else (None if setting == "none" else setting)
        if proxy and not os.environ.get("HTTPS_PROXY"):
            # requests (и обновление токена google-auth) возьмут прокси из окружения
            os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = proxy
            log.info("YouTube: соединение через прокси %s", proxy)

    def _credentials(self, interactive: bool):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        token_path = self.y_path("token")
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            from google.auth.exceptions import RefreshError
            try:
                creds.refresh(Request())
            except RefreshError as e:
                # приложение Google в режиме тестирования: разрешение действует 7 дней, потом — войти заново
                log.warning("Разрешение YouTube истекло или отозвано (%s) — нужно войти заново: python main.py auth", e)
                creds = None
        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError("YouTube не авторизован. Выполните один раз: python main.py auth")
            secrets = self.y_path("client_secrets")
            if not secrets.exists():
                raise RuntimeError(f"Нет файла {secrets} — скачайте OAuth client (Desktop app) из Google Cloud "
                                   "Console, см. README")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(
                port=8765, prompt="consent", open_browser=True,
                authorization_prompt_message="Если браузер не открылся, откройте ссылку вручную:\n{url}\n",
                success_message="Готово! YouTube подключён — эту вкладку можно закрыть.")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def y_path(self, key: str) -> Path:
        from ..config import ROOT

        p = Path(str(self.y.get(key)))
        return p if p.is_absolute() else ROOT / p

    def authorize(self) -> None:
        self._credentials(interactive=True)
        log.info("YouTube авторизован, токен сохранён в %s", self.y_path("token"))

    @property
    def session(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession

            self._session = AuthorizedSession(self._credentials(interactive=False))
        return self._session

    @staticmethod
    def _check(r, what: str) -> None:
        if r.status_code >= 400 and ("quotaExceeded" in r.text or "uploadLimitExceeded" in r.text):
            # YouTube отвечает 400 или 403 — в любом случае это дневной лимит, очередь догрузится завтра
            raise QuotaExceeded("исчерпан дневной лимит загрузок YouTube — остальное загружу при следующем запуске")
        if r.status_code >= 400:
            raise RuntimeError(f"YouTube: {what} — {r.status_code} {r.text[:500]}")

    def upload(self, file: Path, meta: VideoMeta, publish_at: datetime | None) -> str:
        status: dict = {
            "privacyStatus": self.y.privacy,
            "selfDeclaredMadeForKids": bool(self.y.made_for_kids),
        }
        if publish_at:
            # отложенная публикация: YouTube требует private + publishAt, в момент publishAt станет public
            status["privacyStatus"] = "private"
            utc_time = publish_at.astimezone(timezone.utc) if publish_at.tzinfo else publish_at.replace(tzinfo=timezone.utc)
            status["publishAt"] = utc_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "snippet": {
                "title": meta.title[:100],
                "description": meta.description[:5000],
                "tags": meta.tags,
                "categoryId": str(self.y.category_id),
                "defaultLanguage": self.y.language,
                "defaultAudioLanguage": self.y.language,
            },
            "status": status,
        }
        size = file.stat().st_size
        s = self.session
        log.info("Загружаю на YouTube «%s» (%.0f МБ, доступ: %s)…", meta.title, size / 2 ** 20, status["privacyStatus"])
        # 1) открываем сессию загрузки — YouTube возвращает адрес, куда слать файл кусками
        r = s.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                   data=json.dumps(body), timeout=60,
                   headers={"Content-Type": "application/json; charset=UTF-8",
                            "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
        self._check(r, "не удалось начать загрузку")
        url = r.headers["Location"]
        # 2) куски по 8 МБ; после обрыва спрашиваем, сколько YouTube уже получил, и продолжаем с этого места
        sent, retries, last_log = 0, 0, -1
        with open(file, "rb") as f:
            while True:
                try:
                    f.seek(sent)
                    chunk = f.read(CHUNK)
                    end = sent + len(chunk) - 1
                    r = s.put(url, data=chunk, timeout=300,
                              headers={"Content-Range": f"bytes {sent}-{end}/{size}"})
                    if r.status_code in (200, 201):
                        video_id = r.json()["id"]
                        break
                    if r.status_code == 308:                    # кусок принят, ждём следующий
                        rng = r.headers.get("Range")
                        sent = int(rng.split("-")[1]) + 1 if rng else 0
                        pct = int(sent * 100 / size)
                        if pct // 20 != last_log:
                            last_log = pct // 20
                            log.info("  загружено %d%%", pct)
                        retries = 0
                        continue
                    if r.status_code in (500, 502, 503, 504):
                        raise ConnectionError(f"YouTube ответил {r.status_code}")
                    self._check(r, "ошибка при загрузке")
                except (ConnectionError, TimeoutError, OSError) as e:
                    if retries >= 6:
                        raise
                    retries += 1
                    log.info("  обрыв (%s) — продолжу через %d с", type(e).__name__, 2 ** retries)
                    time.sleep(2 ** retries)
                    q = s.put(url, headers={"Content-Range": f"bytes */{size}"}, timeout=60)
                    if q.status_code in (200, 201):
                        video_id = q.json()["id"]
                        break
                    rng = q.headers.get("Range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
        when = f", публикация {publish_at:%d.%m %H:%M} UTC" if publish_at else ""
        log.info("Загружено на YouTube: https://youtu.be/%s%s", video_id, when)
        return video_id
