@echo off
rem Copia de demonstracao: dados ficticios novos a cada inicio, na porta 8001 (o app de verdade fica na 8000).
cd /d "%~dp0"
if "%~1"=="--abrir" goto abrir
set FINANCAS_DB=%~dp0data\demo.db
set FINANCAS_DEMO=1
echo Gerando dados ficticios...
uv run python -m scripts.gerar_demo "%FINANCAS_DB%" || (pause & exit /b 1)
start "" /min cmd /c ""%~f0" --abrir"
uv run uvicorn app.main:criar_app --factory --host 127.0.0.1 --port 8001
goto :eof

:abrir
for /l %%i in (1,1,120) do (
  curl -s -o nul http://127.0.0.1:8001/ && goto pronto
  timeout /t 1 /nobreak >nul
)
:pronto
start "" http://127.0.0.1:8001
exit
