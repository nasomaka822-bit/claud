@echo off
rem Проверка тендеров каждые 30 минут. Чтобы остановить, закройте окно.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    py -3 tender_monitor.py --loop
) else (
    python tender_monitor.py --loop
)
pause
