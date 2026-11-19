@echo off
cd /d "%~dp0"
set "RUNTIME=%LOCALAPPDATA%\ZCodeResumeWatcher\runtime"
set "PYW="

rem ===== 优先 pythonw(无黑窗);找不到再按 uv/便携版顺序探测 =====
where pythonw >nul 2>nul && set "PYW=pythonw" && goto :run
where pyw >nul 2>nul && set "PYW=pyw -3" && goto :run
set "UVW=%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\pythonw.exe"
if exist "%UVW%" set "PYW="%UVW%"" && goto :run
if exist "%RUNTIME%\pythonw.exe" set "PYW="%RUNTIME%\pythonw.exe"" && goto :run
if exist "%RUNTIME%\python.exe" set "PYW="%RUNTIME%\python.exe"" && goto :run

:boot
echo Python runtime not found.
echo This will download the official Python embeddable package to:
echo   %RUNTIME%
echo Green/portable: no registry, no PATH changes; delete the folder to remove.
echo Source: https://www.python.org/ (about 12 MB).
echo Press any key to start, Ctrl+C to cancel...
pause >nul
if not exist "%RUNTIME%" mkdir "%RUNTIME%"
set "URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
echo Downloading...
curl -L --fail --retry 2 -o "%RUNTIME%\python-embed.zip" "%URL%" >nul 2>nul
if errorlevel 1 powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%URL%' -OutFile '%RUNTIME%\python-embed.zip' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 goto :fail
echo Extracting...
tar -xf "%RUNTIME%\python-embed.zip" -C "%RUNTIME%" >nul 2>nul
if errorlevel 1 powershell -NoProfile -Command "Expand-Archive -Force -LiteralPath '%RUNTIME%\python-embed.zip' -DestinationPath '%RUNTIME%'"
del "%RUNTIME%\python-embed.zip" >nul 2>nul
if not exist "%RUNTIME%\pythonw.exe" if not exist "%RUNTIME%\python.exe" goto :fail
if exist "%RUNTIME%\pythonw.exe" (set "PYW="%RUNTIME%\pythonw.exe"") else (set "PYW="%RUNTIME%\python.exe"")
goto :run

:fail
echo.
echo Download failed. Install Python 3 and retry:
echo   https://www.python.org/downloads/
pause
exit /b 1

:run
start "" %PYW% "%~dp0ZCode续命面板.pyw"
exit /b 0
