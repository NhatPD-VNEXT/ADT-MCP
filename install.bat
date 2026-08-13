@echo off
REM ---------------------------------------------------------------------
REM One-time setup after cloning. Creates .venv and installs adt-mcp into
REM it, so `run.bat` works without touching the machine's global Python.
REM
REM   install.bat                create .venv and install into it (recommended)
REM   install.bat --no-venv      install into the Python already on PATH
REM   install.bat --no-browser   skip Playwright (only for basic-auth systems)
REM ---------------------------------------------------------------------
setlocal

cd /d "%~dp0"

set "USE_VENV=1"
set "WITH_BROWSER=1"
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-venv" set "USE_VENV=0"
if /i "%~1"=="--no-browser" set "WITH_BROWSER=0"
shift
goto parse_args
:args_done

echo.
echo === ADT MCP setup ===
echo Project: %~dp0
echo.

REM --- 1. Find a Python 3.10+ interpreter -------------------------------
set "PY="
call :find_python
if not defined PY (
    echo [ERROR] No Python 3.10 or newer was found on this machine.
    echo         Install it from https://www.python.org/downloads/windows/
    echo         ^(tick "Add python.exe to PATH"^), then run install.bat again.
    exit /b 1
)
for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%V"
echo Found Python %PYVER% via "%PY%"

REM --- 2. Interpreter to install into -----------------------------------
set "PYEXE="
if "%USE_VENV%"=="1" call :make_venv
if not "%USE_VENV%"=="1" (
    for /f "delims=" %%E in ('%PY% -c "import sys;print(sys.executable)"') do set "PYEXE=%%E"
)
if not defined PYEXE exit /b 1
echo Installing into: %PYEXE%
echo.

REM --- 3. Dependencies ---------------------------------------------------
"%PYEXE%" -m pip install --upgrade pip --disable-pip-version-check -q
if errorlevel 1 goto :pipfail

REM Cookie systems log in through Playwright, so install it by default. It
REM drives the machine's own Chrome/Edge, so no browser download is needed.
if "%WITH_BROWSER%"=="1" (
    echo Installing adt-mcp editable, with browser login support ...
    "%PYEXE%" -m pip install -e ".[refresh]" --disable-pip-version-check
) else (
    echo Installing adt-mcp editable, without browser login support ...
    "%PYEXE%" -m pip install -e . --disable-pip-version-check
)
if errorlevel 1 goto :pipfail

REM --- 4. Verify ---------------------------------------------------------
echo.
REM Import adt_mcp.server, not just adt_mcp: that is what pulls in FastMCP,
REM starlette and httpx, so an incompatible dependency fails here and not
REM later at startup.
"%PYEXE%" -c "import adt_mcp, adt_mcp.server; print('OK - adt_mcp', adt_mcp.__version__)"
if errorlevel 1 (
    echo [ERROR] The package still does not import - see the message above.
    exit /b 1
)

if "%WITH_BROWSER%"=="1" (
    "%PYEXE%" -c "import playwright; print('OK - playwright ready for cookie logins')"
    if errorlevel 1 echo [WARN] Playwright missing: cookie logins will not work.
)

echo.
echo === Setup complete ===
if not exist "%~dp0systems.json" (
    echo NOTE: no systems.json yet - this machine has no SAP system configured.
    echo       Start run.bat and add the system in the web admin, or copy your
    echo       systems.json ^(plus the cookies\ folder^) from another machine.
    echo       systems.json is gitignored on purpose: it holds credentials.
)
echo Start the server with:  run.bat
echo.
exit /b 0

REM ----------------------------------------------------------------------
:find_python
REM Sets PY to the first launcher that reports Python >= 3.10. The
REM Microsoft Store stub fails this check and is skipped automatically.
for %%C in ("py -3" "python" "python3") do (
    %%~C -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PY=%%~C"
        goto :eof
    )
)
goto :eof

:make_venv
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo [ERROR] Could not create .venv - see the message above.
        goto :eof
    )
)
set "PYEXE=%~dp0.venv\Scripts\python.exe"
goto :eof

:pipfail
echo.
echo [ERROR] pip failed - see the message above (offline or proxy?).
exit /b 1
