@echo off
chcp 65001 >nul
rem Ежедневный запуск: догрузить очередь на YouTube и, если в расписании мало роликов, смонтировать следующие серии.
rem Запускается Планировщиком заданий Windows (задача «editbot — сериал»). Журнал — logs\editbot.log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" main.py batch --dir "library" --title "Менталист" --count 3 --keep-ahead 6 --limit 2 >> "logs\daily.log" 2>&1
