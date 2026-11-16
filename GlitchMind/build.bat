@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   GlitchMind: сборка GlitchMind.exe
echo ============================================

set PYEXE=
for %%V in (3.12 3.11 3.10) do (
  if not defined PYEXE (
    py -%%V -c "import sys" >nul 2>&1 && set PYEXE=py -%%V
  )
)
if not defined PYEXE (
  python -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,12)" >nul 2>&1 && set PYEXE=python
)
if not defined PYEXE (
  echo.
  echo [!] Не найден Python 3.10 - 3.12.
  echo     Установи Python 3.12 с https://www.python.org/downloads/
  echo     ^(поставь галочку "Add python.exe to PATH"^) и запусти build.bat снова.
  pause
  exit /b 1
)
echo Использую: %PYEXE%

if not exist .venv (
  %PYEXE% -m venv .venv
  if errorlevel 1 ( echo [!] Не удалось создать окружение & pause & exit /b 1 )
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 ( echo [!] Не удалось установить зависимости. Проверь интернет. & pause & exit /b 1 )

echo.
echo --- Тесты ---
python -m pytest -q
if errorlevel 1 echo [!] Часть тестов не прошла — сборка продолжится, но проверь вывод выше.

echo.
echo --- Сборка exe ---
pyinstaller --noconfirm --clean glitchmind.spec
if errorlevel 1 ( echo [!] Сборка не удалась & pause & exit /b 1 )

echo.
echo --- Самопроверка exe ---
dist\GlitchMind\GlitchMind.exe --selftest
if exist dist\GlitchMind\selftest.txt type dist\GlitchMind\selftest.txt

echo.
echo ============================================
echo   ГОТОВО:  dist\GlitchMind\GlitchMind.exe
echo   Копируй всю папку dist\GlitchMind целиком.
echo ============================================
pause
