@echo off
REM Окно НЕ закроется само — даже если будет ошибка
cd /d "%~dp0"
cmd /k run.bat
