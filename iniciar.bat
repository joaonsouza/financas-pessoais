@echo off
cd /d "%~dp0"
if "%~1"=="--abrir" goto abrir
rem Abre o navegador so quando o servidor responder (a subida pode levar ~30 s).
start "" /min cmd /c ""%~f0" --abrir"
uv run uvicorn app.main:criar_app --factory --host 127.0.0.1 --port 8000
goto :eof

:abrir
for /l %%i in (1,1,120) do (
  curl -s -o nul http://127.0.0.1:8000/ && goto pronto
  timeout /t 1 /nobreak >nul
)
:pronto
start "" http://127.0.0.1:8000
exit
