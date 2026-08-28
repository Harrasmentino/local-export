@echo off
setlocal
cd /d "%~dp0"
set "AGENT_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%AGENT_PY%" (
  echo Local Python is missing. See README.md.
  exit /b 1
)
"%AGENT_PY%" -X utf8 "%~dp0agent_export.py" %*
set "AGENT_RESULT=%errorlevel%"
if not "%LOCAL_EXPORT_NO_PAUSE%"=="1" pause
exit /b %AGENT_RESULT%
