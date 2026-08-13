@echo off
REM Start ADT MCP server and open the web admin in the browser.
REM Run install.bat once after cloning; this script only launches.
setlocal

cd /d "%~dp0"

REM Port is configurable: set ADT_MCP_PORT before calling, or edit here.
if not defined ADT_MCP_PORT set "ADT_MCP_PORT=8765"
set "PORT=%ADT_MCP_PORT%"
set "URL=http://127.0.0.1:%PORT%/"

REM --- Interpreter: prefer the .venv that install.bat created ------------
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :find_python
if not defined PYEXE (
    echo [ERROR] No Python 3.10 or newer was found. Run install.bat first.
    exit /b 1
)

REM --- The package lives under src\ (src-layout). Put it on the path so a
REM --- checkout runs even when it was never pip-installed. Append instead
REM --- of overwriting, and never leave a trailing ';' - an empty PYTHONPATH
REM --- entry silently adds the current directory to sys.path.
if defined PYTHONPATH (
    set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%~dp0src"
)

"%PYEXE%" -c "import adt_mcp, mcp, httpx" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] adt-mcp or its dependencies are missing for:
    echo             %PYEXE%
    echo         Run install.bat once, then start run.bat again.
    exit /b 1
)

REM --- Kill any old server still holding the port so the new code is loaded.
REM --- Only listening sockets (foreign address 0.0.0.0:0 or [::]:0) count,
REM --- so a localized netstat cannot make us kill a browser connection.
for /f "tokens=2,3,5" %%A in ('netstat -ano -p TCP ^| findstr /r /c:":%PORT% "') do (
    if "%%B"=="0.0.0.0:0" (
        echo Killing old server PID %%C on port %PORT%
        taskkill /f /pid %%C >nul 2>&1
    )
    if "%%B"=="[::]:0" (
        echo Killing old server PID %%C on port %PORT%
        taskkill /f /pid %%C >nul 2>&1
    )
)

REM Open the browser after a short delay so the server has time to bind.
REM ping instead of timeout: timeout.exe aborts when stdin is redirected,
REM and a Git-Bash PATH shadows it with a GNU timeout that has other flags.
start "" /b cmd /c ""%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul & start "" %URL%"

echo Starting ADT MCP on %URL%  (MCP at /mcp, admin at /)
"%PYEXE%" -m adt_mcp

exit /b %errorlevel%

REM ----------------------------------------------------------------------
:find_python
REM Sets PYEXE to the first launcher reporting Python >= 3.10. The Microsoft
REM Store stub fails this check and is skipped automatically.
for %%C in ("py -3" "python" "python3") do (
    %%~C -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%E in ('%%~C -c "import sys;print(sys.executable)"') do set "PYEXE=%%E"
        goto :eof
    )
)
goto :eof
