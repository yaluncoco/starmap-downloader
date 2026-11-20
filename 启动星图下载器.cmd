@echo off
setlocal
pushd "%~dp0"
if exist "dist\StarmapDownloader\StarmapDownloader.exe" (
  start "" "dist\StarmapDownloader\StarmapDownloader.exe"
  exit /b 0
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py
) else (
  echo Install the requirements or extract the prebuilt Windows archive first. See README.md.
  pause
  exit /b 1
)
if errorlevel 1 pause
