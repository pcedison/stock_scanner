@echo off
setlocal

if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

python -m pip show fastapi >nul 2>nul
if errorlevel 1 (
  echo Missing Python packages. Please run:
  echo python -m pip install -r requirements.txt
  pause
  exit /b 1
)

python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
