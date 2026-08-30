@echo off
chcp 65001 >nul
title Transcritor V10 Auditoria
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
  set PY=py
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (
    set PY=python
  ) else (
    echo Python nao encontrado.
    pause
    exit /b 1
  )
)

echo ============================================================
echo TRANSCRITOR V10 AUDITORIA
echo ============================================================
echo Conferindo dependencias...

%PY% -c "import reportlab,imageio_ffmpeg,requests" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias...
  %PY% -m pip install --upgrade pip
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ERRO NA INSTALACAO.
    pause
    exit /b 1
  )
)

for /r "whisper" %%F in (whisper-cli.exe) do (
  set "WHISPER_FOUND=%%F"
  goto :found
)

:found
if not defined WHISPER_FOUND (
  echo ERRO: whisper-cli.exe nao encontrado no pacote.
  pause
  exit /b 1
)

echo Abrindo V10...
%PY% app.py
pause
