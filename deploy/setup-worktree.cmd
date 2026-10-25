@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pip install --no-deps --disable-pip-version-check .
if errorlevel 1 exit /b 1

endlocal
