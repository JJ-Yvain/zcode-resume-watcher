@echo off
title ZCode 自动续命监视器 - 运行中(关闭本窗口即停止)
cd /d "%~dp0"
rem ===== Python 探测与自动引导 =====
set "PYCT="
set "RUNTIME=%LOCALAPPDATA%\ZCodeResumeWatcher\runtime"
set "PYCT=%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"
if exist "%PYCT%" goto :run
where python >nul 2>nul && set "PYCT=python" && goto :run
where py >nul 2>nul && set "PYCT=py" && goto :run
if exist "%RUNTIME%\python.exe" set "PYCT=%RUNTIME%\python.exe" && goto :run

:bootstrap
set "PYCT="
echo 未检测到 Python 运行环境。
echo 本工具将自动下载 Python 官方便携版并解压到您的用户目录:
echo   %RUNTIME%
echo 绿色免安装:不修改系统设置、注册表与 PATH,随时可整个删除。
echo 下载来源: https://www.python.org/ 官方(约 12MB)。
echo 按任意键开始,按 Ctrl+C 取消...
pause >nul
if not exist "%RUNTIME%" mkdir "%RUNTIME%"
set "URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
echo 正在下载...
curl -L --fail --retry 2 -o "%RUNTIME%\python-embed.zip" "%URL%" >nul 2>nul
if errorlevel 1 powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%URL%' -OutFile '%RUNTIME%\python-embed.zip' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 goto :dlfail
echo 正在解压...
tar -xf "%RUNTIME%\python-embed.zip" -C "%RUNTIME%" >nul 2>nul
if errorlevel 1 powershell -NoProfile -Command "Expand-Archive -Force -LiteralPath '%RUNTIME%\python-embed.zip' -DestinationPath '%RUNTIME%'"
del "%RUNTIME%\python-embed.zip" >nul 2>nul
if not exist "%RUNTIME%\python.exe" goto :dlfail
set "PYCT=%RUNTIME%\python.exe"
"%PYCT%" --version >nul 2>nul
if errorlevel 1 goto :dlfail
echo Python 便携版就绪。
goto :run

:dlfail
echo.
echo 自动下载失败。请手动安装 Python 3 后重试:
echo   https://www.python.org/downloads/
pause
exit /b 1
:run
echo ============================================
echo  ZCode 自动续命监视器
echo  正在运行:模型断连/空响应后自动发送续命消息
echo  - 最小化本窗口即可,勿关闭
echo  - 双击多次不会重复启动
echo  本窗口内会实时显示监视日志
echo ============================================
echo.
if "%PYCT%"=="py" (
  py -3 zcode_resume_watcher.py
) else (
  "%PYCT%" zcode_resume_watcher.py
)
echo.
echo === 监视已停止(关闭本窗口不会影响 ZCode)===
pause
