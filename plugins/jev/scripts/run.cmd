@echo off
setlocal

rem Resolve a Python that can import system_one_adapter (the engine's only
rem online dependency): JEV_PYTHON, then jev-starter venv, then py/python.
if defined JEV_PYTHON if exist "%JEV_PYTHON%" (
  set "PY=%JEV_PYTHON%"
  goto run
)
if exist "%USERPROFILE%\jev-starter\.venv\Scripts\python.exe" (
  set "PY=%USERPROFILE%\jev-starter\.venv\Scripts\python.exe"
  goto run
)
where py >nul 2>&1 && (
  set "PY=py"
  set "PYARG=-3"
  goto run
)
where python >nul 2>&1 && (
  set "PY=python"
  goto run
)

echo Jev MCP: no Python interpreter found. Set JEV_PYTHON to a Python with "system-one-adapter[openai]" installed. 1>&2
exit /b 127

:run
"%PY%" %PYARG% "%~dp0server.py" %*
endlocal
