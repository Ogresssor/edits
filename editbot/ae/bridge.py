"""Мост к Adobe After Effects: собирает проект .aep по описанию монтажа и рендерит его.

    timeline (dict) -> скрипт ExtendScript (template.jsx + данные) -> AfterFX.exe -r скрипт -> проект .aep
    -> aerender.exe -> mp4

Скрипты запускаются из папки с латинским путём (edit.ae_bridge_dir, по умолчанию C:\\aebridge): так надёжнее
для ExtendScript. Скриптам After Effects по умолчанию запрещено писать файлы, поэтому об ошибке скрипт
сообщает отдельным проектом с композицией «ERR|…|», который читается через aepstrings.
Если After Effects уже открыт (вы в нём работаете), сборка не запускается — чтобы не закрыть ваш проект.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .. import ffmpeg
from .aepstrings import strings

log = logging.getLogger("ae")

TEMPLATE = Path(__file__).with_name("template.jsx")


class AEError(RuntimeError):
    pass


def find_ae(cfg_path: str | None = None) -> Path | None:
    """Папка «Support Files» установленного After Effects (самая новая версия)."""
    if cfg_path and Path(cfg_path).exists():
        return Path(cfg_path)
    roots = sorted(Path("C:/Program Files/Adobe").glob("Adobe After Effects */Support Files"), reverse=True)
    return next((r for r in roots if (r / "AfterFX.exe").exists()), None)


def ae_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AfterFX.exe", "/NH"], capture_output=True, text=True,
                         encoding="cp866", errors="ignore").stdout
    return "AfterFX.exe" in out


def crash_dialog(pid: int) -> bool:
    """After Effects после аварийного закрытия при запуске показывает окно «Параметры восстановления после сбоя»
    и ждёт ответа (скрипт не выполняется). Окно — стандартный диалог (#32770) без заголовка."""
    import ctypes
    import ctypes.wintypes as W
    u, found = ctypes.windll.user32, []

    def cb(h, _):
        owner, cls = W.DWORD(), ctypes.create_unicode_buffer(64)
        u.GetWindowThreadProcessId(h, ctypes.byref(owner))
        if owner.value == pid and u.IsWindowVisible(h) and u.GetClassNameW(h, cls, 64) and cls.value == "#32770":
            found.append(h)
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)(cb), 0)
    return bool(found)


def kill_leftovers() -> None:
    """Закрывает зависшие процессы рендера After Effects (запущенные приложением — когда AE не открыт у вас)."""
    # только процессы рендера/скриптов приложения (aerender и After Effects, запущенный с -noui / -r);
    # After Effects, открытый вами из меню «Пуск», не трогаем
    # AfterFX.com — консольный помощник, которого запускает aerender (у вас в работе его не бывает)
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'aerender.exe' -or $_.Name -eq 'AfterFX.com' -or "
          "(($_.Name -like 'AfterFX*') -and ($_.CommandLine -match ' -noui| -r ')) } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


class AfterEffects:
    def __init__(self, support_dir: Path, bridge_dir: Path, mem_cache: int = 20, mem_max: int = 35):
        self.dir = support_dir
        self.bridge = bridge_dir
        self.bridge.mkdir(parents=True, exist_ok=True)
        # без ограничения After Effects при рендере забирает под кэш кадров почти всю оперативную память
        self.mem = (int(mem_cache), int(mem_max))

    def build(self, data: dict, aep_out: Path, timeout: float = 600) -> Path:
        """Собирает проект по данным монтажа и копирует его в aep_out. Возвращает путь к проекту."""
        if ae_running():
            raise AEError("After Effects уже открыт — закройте его, чтобы я собрал проект (иначе закрою вашу работу)")
        job = uuid.uuid4().hex[:8]
        aep = self.bridge / f"job_{job}.aep"
        err = self.bridge / f"job_{job}_ERR.aep"
        data = dict(data, aep=aep.as_posix(), err_aep=err.as_posix(), quit=True)
        jsx = self.bridge / f"job_{job}.jsx"
        code = "var DATA = " + json.dumps(data, ensure_ascii=False) + ";\n" + TEMPLATE.read_text(encoding="utf-8")
        jsx.write_text(code, encoding="utf-8-sig")       # с BOM — иначе ExtendScript читает кириллицу как cp1251
        log.info("After Effects: собираю проект (%d кусков, %d субтитров)…", len(data["clips"]), len(data["subs"]))
        proc = subprocess.Popen([str(self.dir / "AfterFX.exe"), "-r", str(jsx)])
        t0 = time.time()
        warned = False
        while time.time() - t0 < timeout:
            if not warned and time.time() - t0 > 15 and crash_dialog(proc.pid):
                # окно «Параметры восстановления после сбоя» (прошлый AE закрыт принудительно). Перезапуск не помогает
                # (25.10: новый AE висит без окон) — нужен человек: «Продолжить», потом AE закрыть крестиком
                warned = True
                log.warning("After Effects ждёт ответа в окне «Параметры восстановления после сбоя» — "
                            "нажмите «Продолжить» (не безопасный режим: в нём скрипты не работают)")
            if err.exists():
                time.sleep(1.0)
                msg = next((s for s in strings(err) if s.startswith("ERR|")), "ERR|неизвестная ошибка|")
                raise AEError("скрипт After Effects: " + msg.strip("|")[4:])
            if aep.exists() and not ae_running():
                break
            if aep.exists() and time.time() - aep.stat().st_mtime > 60:
                # проект сохранён, а After Effects не закрывается (бывает: app.quit() не срабатывает, AE просто
                # стоит открытым) — проект готов, закрываем AE сами
                log.warning("After Effects сохранил проект, но не закрылся за минуту — закрываю")
                proc.kill()
                time.sleep(3.0)
                break
            time.sleep(2.0)
        else:
            proc.kill()
            raise AEError(f"After Effects не собрал проект за {timeout:.0f} с")
        aep_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(aep, aep_out)
        log.info("After Effects: проект %s (%.0f с)", aep_out.name, time.time() - t0)
        return aep_out

    def render(self, aep: Path, expect: Path, timeout: float = 900, expected_duration: float | None = None) -> Path:
        """Рендер очереди проекта через aerender (без интерфейса). expect — файл, который указан в очереди.
        Ролик ~30 с рендерится около минуты; дольше timeout — значит, aerender завис (бывает: висит без
        нагрузки, а его помощник AfterFX.com держит процесс) — закрываем всё и сообщаем об ошибке."""
        if expect.exists():
            raise AEError(f"Файл рендера уже существует; нужен новый путь, чтобы не принять старый результат: {expect}")
        log.info("After Effects: рендер %s…", aep.name)
        t0 = time.time()
        log_file = self.bridge / f"render_{uuid.uuid4().hex[:8]}.log"
        hung = False
        # вывод — в файл, не в канал: иначе зависший AfterFX.com держит канал и ожидание не кончается
        with open(log_file, "wb") as lf:
            proc = subprocess.Popen([str(self.dir / "aerender.exe"), "-project", str(aep), "-sound", "ON",
                                     "-mem_usage", str(self.mem[0]), str(self.mem[1])], stdout=lf, stderr=lf)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                hung = True
                log.warning("After Effects: рендер завис (%.0f мин без результата) — закрываю", timeout / 60)
                kill_leftovers()
                proc.kill()
            finally:
                time.sleep(2.0)
                if ae_running():               # процесс рендера не закрылся — освобождаем память
                    log.info("After Effects: процесс рендера не закрылся сам — закрываю")
                kill_leftovers()
        out = log_file.read_bytes().decode("cp866", "ignore")
        if hung:
            raise AEError(f"aerender завис и был закрыт; журнал: {log_file}")
        if proc.returncode != 0:
            raise AEError(f"aerender завершился с кодом {proc.returncode}; журнал: {log_file}\n" + out[-2000:])
        if not expect.exists() or expect.stat().st_size < 100_000:
            raise AEError(f"aerender не выдал ролик; журнал: {log_file}\n" + out[-2000:])
        if expect.stat().st_mtime < t0 - 2:
            raise AEError(f"Файл рендера старше текущего запуска; журнал: {log_file}")
        info = ffmpeg.probe(expect)
        if expected_duration is not None and abs(info.duration - expected_duration) > max(.15, 2 / info.fps):
            raise AEError(f"Неполный рендер: {info.duration:.3f} с вместо {expected_duration:.3f} с; журнал: {log_file}")
        receipt = dict(project=str(aep.resolve()), output=str(expect.resolve()), returncode=proc.returncode,
                       started_at_unix=t0, finished_at_unix=time.time(), duration=info.duration,
                       expected_duration=expected_duration, bytes=expect.stat().st_size)
        expect.with_suffix('.render.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        log_file.unlink(missing_ok=True)
        log.info("After Effects: рендер готов за %.0f с", time.time() - t0)
        return expect
