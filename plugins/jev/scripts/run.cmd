@echo off
setlocal

rem Resolve a Python that can import system_one_adapter (the Jev engine's only
rem online dependency). Override with JEV_PYTHON if you keep it elsewhere.
rem Prefer an explicit interpreter, then the jev-starter venv, then py/python.
if defined JEV_PYTHON if exist "%JEV_PYTHON%" (
  "%JEV_PYTHON%" "%~dp0server.py" %*
  endlocal
  exit /b
)

if defined USERPROFILE if exist "%USERPROFILE%\jev-starter\.venv\Scripts\python.exe" (
  "%USERPROFILE%\jev-starter\.venv\Scripts\python.exe" "%~dp0server.py" %*
  endlocal
  exit /b
)

where py >nul 2>&1 && (
  py -3 "%~dp0server.py" %*
  endlocal
  exit /b
)
where python >nul 2>&1 && (
  python "%~dp0server.py" %*
  endlocal
  exit /b
)

echo Jev MCP: no Python interpreter found. Set JEV_PYTHON to a Python with "system-one-adapter[openai]" installed. 1>&2
exit /b 127
