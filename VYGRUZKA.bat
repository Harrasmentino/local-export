@echo off
setlocal
cd /d "%~dp0"
set "RUNTIME=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies"
set "PY=%RUNTIME%\python\python.exe"
set "NODE=%RUNTIME%\node\bin\node.exe"
set "MODULES=%RUNTIME%\node\node_modules"
if defined LOCAL_EXPORT_NODE set "NODE=%LOCAL_EXPORT_NODE%"
if defined LOCAL_EXPORT_MODULES set "MODULES=%LOCAL_EXPORT_MODULES%"
if not exist "%PY%" goto missing
if not exist "%NODE%" goto missing
if not exist "%MODULES%\@oai\artifact-tool\package.json" goto missing
if not exist "%~dp0_local\node_modules" (
  mklink /J "%~dp0_local\node_modules" "%MODULES%" >nul
  if errorlevel 1 goto missing
)
echo Fresh Confluence reports. No AI. No cached-data fallback.
echo Credentials are stored in Windows Credential Manager after a successful download.
echo.
"%PY%" -X utf8 "%~dp0local_export.py" %*
set "RESULT=%errorlevel%"
goto finish
:missing
echo Required local runtime or libraries are missing.
echo See README_LOCAL.md. No automatic downloads are performed.
set "RESULT=1"
:finish
echo.
if not "%LOCAL_EXPORT_NO_PAUSE%"=="1" pause
exit /b %RESULT%
