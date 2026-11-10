@echo off
cd /d "%~dp0"
tools\tailwindcss.exe -i app\web\static\input.css -o app\web\static\app.css --minify
