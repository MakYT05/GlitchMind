@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
rem Запуск из исходников (без сборки exe). Нужен Python 3.10 - 3.12.
if not exist .venv (
  py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -r requirements.txt
python main.py %*
