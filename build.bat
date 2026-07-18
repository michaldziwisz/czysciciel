@echo off
REM ============================================================
REM  Czysciciel - build.bat  (PyInstaller onedir)
REM  Buduje lekki launcher GUI. Ciezkie klocki (torch, model,
REM  ffmpeg) dociagane sa przy pierwszym uruchomieniu (bootstrap).
REM ============================================================
cd /d C:\czysciciel-app

set PY="C:\Program Files\Python312\python.exe"

echo === czyszczenie poprzedniego buildu ===
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist Czysciciel.spec del /q Czysciciel.spec

echo === PyInstaller ===
%PY% -m PyInstaller ^
  --name Czysciciel ^
  --windowed ^
  --noconfirm ^
  --clean ^
  --add-data "bootstrap.py;." ^
  --add-data "worker.py;." ^
  --add-data "gui.py;." ^
  --collect-submodules wx ^
  czysciciel.py

echo EXITCODE=%errorlevel%
