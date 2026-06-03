@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "LUCID_MODE=hub"
if "%LUCID_HOST%"=="" set "LUCID_HOST=127.0.0.1"
if "%LUCID_PORT%"=="" set "LUCID_PORT=21893"
if "%PYTHONUTF8%"=="" set "PYTHONUTF8=1"
if "%PYTHONIOENCODING%"=="" set "PYTHONIOENCODING=utf-8"

set "PYTHON_BOOTSTRAP_VERSION=3.10.11"
set "OLD_RUNTIME_DIR=%CD%\.fleet-runtime"
set "RUNTIME_DIR=%CD%\.lucid-runtime"
if not exist "%RUNTIME_DIR%" (
    if exist "%OLD_RUNTIME_DIR%" (
        move "%OLD_RUNTIME_DIR%" "%RUNTIME_DIR%" >nul
        if errorlevel 1 (
            echo [LUCID] Failed to migrate %OLD_RUNTIME_DIR% to %RUNTIME_DIR%.
            pause
            exit /b 1
        )
    )
)
set "BUNDLED_PYTHON_DIR=%RUNTIME_DIR%\python"
set "BUNDLED_PYTHON=%BUNDLED_PYTHON_DIR%\python.exe"
set "BOOTSTRAP_PYTHON=0"

if not "%LUCID_PYTHON%"=="" (
    set "PYTHON_CMD=%LUCID_PYTHON%"
    set "PYTHON_ARGS="
) else (
    where py.exe >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=py"
        set "PYTHON_ARGS=-3"
    ) else (
        set "PYTHON_CMD=python"
        set "PYTHON_ARGS="
    )
)

if "%LUCID_USE_BUNDLED_PYTHON%"=="1" (
    set "BOOTSTRAP_PYTHON=1"
) else (
    "%PYTHON_CMD%" %PYTHON_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    if errorlevel 1 set "BOOTSTRAP_PYTHON=1"
)

if "%BOOTSTRAP_PYTHON%"=="1" (
    call :ensure_bundled_python
    if errorlevel 1 (
        pause
        exit /b 1
    )
    set "PYTHON_CMD=%BUNDLED_PYTHON%"
    set "PYTHON_ARGS="
)

set "VENV_DIR=%CD%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
if exist "%VENV_DIR%" (
    if not exist "%VENV_PYTHON%" (
        echo [LUCID] existing .venv is incomplete. Recreating it...
        rmdir /s /q "%VENV_DIR%"
        if exist "%VENV_DIR%" (
            echo [LUCID] Failed to remove incomplete .venv.
            pause
            exit /b 1
        )
    )
)

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    if errorlevel 1 (
        echo [LUCID] existing .venv uses an unsupported Python version. Recreating it...
        rmdir /s /q "%VENV_DIR%"
        if exist "%VENV_DIR%" (
            echo [LUCID] Failed to remove incompatible .venv.
            pause
            exit /b 1
        )
    )
)

if not exist "%VENV_PYTHON%" (
    echo [LUCID] creating Windows hub venv...
    "%PYTHON_CMD%" %PYTHON_ARGS% -m venv .venv
    if errorlevel 1 (
        echo [LUCID] Failed to create .venv.
        pause
        exit /b 1
    )
)

"%VENV_PYTHON%" -c "import fastapi, importlib.metadata as metadata; metadata.version('LUCID')" >nul 2>nul
if errorlevel 1 (
    echo [LUCID] installing hub dependencies...
    "%VENV_PYTHON%" -m pip install -q --upgrade pip
    if errorlevel 1 (
        echo [LUCID] Failed to upgrade pip.
        pause
        exit /b 1
    )
    "%VENV_PYTHON%" -m pip install -q -e .
    if errorlevel 1 (
        echo [LUCID] Failed to install Python dependencies into .venv.
        pause
        exit /b 1
    )
)

"%VENV_PYTHON%" "scripts\check_port.py" --host "%LUCID_HOST%" --port "%LUCID_PORT%"
if errorlevel 1 (
    pause
    exit /b 1
)

echo [LUCID] mode=hub starting on http://%LUCID_HOST%:%LUCID_PORT%
"%VENV_PYTHON%" -m uvicorn app:app --host "%LUCID_HOST%" --port "%LUCID_PORT%" --reload --no-use-colors
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [LUCID] hub exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

:ensure_bundled_python
if exist "%BUNDLED_PYTHON%" (
    "%BUNDLED_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    if not errorlevel 1 exit /b 0
)

set "PYTHON_PACKAGE_ARCH=amd64"
if /I "%PROCESSOR_ARCHITECTURE%"=="x86" (
    if "%PROCESSOR_ARCHITEW6432%"=="" set "PYTHON_PACKAGE_ARCH=win32"
)

if "%PYTHON_PACKAGE_ARCH%"=="win32" (
    set "PYTHON_PACKAGE_NAME=pythonx86.%PYTHON_BOOTSTRAP_VERSION%.nupkg"
    set "PYTHON_PACKAGE_URL=https://www.nuget.org/api/v2/package/pythonx86/%PYTHON_BOOTSTRAP_VERSION%"
) else (
    set "PYTHON_PACKAGE_NAME=python.%PYTHON_BOOTSTRAP_VERSION%.nupkg"
    set "PYTHON_PACKAGE_URL=https://www.nuget.org/api/v2/package/python/%PYTHON_BOOTSTRAP_VERSION%"
)

set "DOWNLOAD_DIR=%RUNTIME_DIR%\downloads"
set "PYTHON_PACKAGE=%DOWNLOAD_DIR%\%PYTHON_PACKAGE_NAME%"
set "PYTHON_PACKAGE_ZIP=%DOWNLOAD_DIR%\%PYTHON_PACKAGE_NAME%.zip"
set "PYTHON_EXTRACT_DIR=%RUNTIME_DIR%\python-extract"

if not exist "%DOWNLOAD_DIR%" mkdir "%DOWNLOAD_DIR%"

if not exist "%PYTHON_PACKAGE%" (
    echo [LUCID] Python 3.10+ was not found. Downloading Python runtime %PYTHON_BOOTSTRAP_VERSION%...
    call :download_file "%PYTHON_PACKAGE_URL%" "%PYTHON_PACKAGE%"
    if errorlevel 1 (
        echo [LUCID] Failed to download Python runtime from:
        echo [LUCID] %PYTHON_PACKAGE_URL%
        exit /b 1
    )
)

where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo [LUCID] powershell.exe is required to extract bundled Python.
    exit /b 1
)

if exist "%PYTHON_EXTRACT_DIR%" rmdir /s /q "%PYTHON_EXTRACT_DIR%"
if exist "%BUNDLED_PYTHON_DIR%" rmdir /s /q "%BUNDLED_PYTHON_DIR%"
if exist "%PYTHON_PACKAGE_ZIP%" del /q "%PYTHON_PACKAGE_ZIP%"

echo [LUCID] extracting bundled Python into %BUNDLED_PYTHON_DIR%...
copy /y "%PYTHON_PACKAGE%" "%PYTHON_PACKAGE_ZIP%" >nul
if errorlevel 1 (
    echo [LUCID] Failed to prepare Python runtime archive:
    echo [LUCID] %PYTHON_PACKAGE_ZIP%
    exit /b 1
)

mkdir "%PYTHON_EXTRACT_DIR%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath $env:PYTHON_PACKAGE_ZIP -DestinationPath $env:PYTHON_EXTRACT_DIR -Force"
if errorlevel 1 (
    echo [LUCID] Failed to extract Python runtime archive:
    echo [LUCID] %PYTHON_PACKAGE%
    exit /b 1
)

if not exist "%PYTHON_EXTRACT_DIR%\tools\python.exe" (
    echo [LUCID] Python runtime archive did not contain:
    echo [LUCID] %PYTHON_EXTRACT_DIR%\tools\python.exe
    exit /b 1
)

move "%PYTHON_EXTRACT_DIR%\tools" "%BUNDLED_PYTHON_DIR%" >nul
if errorlevel 1 (
    echo [LUCID] Failed to install Python runtime into:
    echo [LUCID] %BUNDLED_PYTHON_DIR%
    exit /b 1
)
rmdir /s /q "%PYTHON_EXTRACT_DIR%" >nul 2>nul

if not exist "%BUNDLED_PYTHON%" (
    echo [LUCID] Python runtime extraction finished, but python.exe was not found at:
    echo [LUCID] %BUNDLED_PYTHON%
    exit /b 1
)

"%BUNDLED_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [LUCID] Bundled Python is not compatible.
    exit /b 1
)
exit /b 0

:download_file
set "DOWNLOAD_URL=%~1"
set "DOWNLOAD_TARGET=%~2"

where curl.exe >nul 2>nul
if not errorlevel 1 (
    curl.exe -L --fail -o "%DOWNLOAD_TARGET%" "%DOWNLOAD_URL%"
    if not errorlevel 1 exit /b 0
)

where powershell.exe >nul 2>nul
if not errorlevel 1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile '%DOWNLOAD_TARGET%' -UseBasicParsing"
    if not errorlevel 1 exit /b 0
)

echo [LUCID] curl.exe or powershell.exe is required to download bundled Python.
exit /b 1
