@echo off
REM Seam - стартиране на Windows
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [Seam] Създаване на виртуална среда...
  py -3 -m venv .venv
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip Flask waitress
)

REM waitress е продукционният сървър; ако липсва (стара среда), го добавяме.
.venv\Scripts\python.exe -c "import waitress" 2>nul || (
  echo [Seam] Инсталиране на waitress...
  .venv\Scripts\python.exe -m pip install --quiet waitress
)

if not exist "seam.db" (
  echo [Seam] Зареждане на демо данни...
  .venv\Scripts\python.exe seed.py
)

echo [Seam] Стартиране на http://127.0.0.1:5000
.venv\Scripts\python.exe app.py
endlocal
