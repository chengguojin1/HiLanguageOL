@echo off
rem ============================================================
rem  HiLanguageOL 一键打包脚本
rem    1) 环境自检（虚拟环境 / PyInstaller / Inno Setup / 程序是否在运行）
rem    2) 版本号：直接回车 = 自动 +1，也可手填（installer.iss 与 version_info.txt 自动同步）
rem    3) PyInstaller 打包 exe
rem    4) Inno Setup 生成安装包
rem    5) 安检：确认没有把 API Key 等敏感信息打进包
rem
rem  双击运行即可；命令行静默运行： build.bat /auto
rem ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul
cd /d "%~dp0"
if /i "%~1"=="/auto" (set "AUTO=1") else (set "AUTO=")
set "SYS=%SystemRoot%\System32"

echo ============================================================
echo   HiLanguageOL 打包
echo ============================================================

rem ---------- 1. 环境自检 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没有找到项目虚拟环境 .venv
    echo        请先在本目录执行：
    echo            python -m venv .venv
    echo            .venv\Scripts\pip install -r requirements.txt
    goto FAIL
)
set "PY=.venv\Scripts\python.exe"

"%SYS%\tasklist.exe" /fi "imagename eq HiLanguageOL.exe" 2>nul | "%SYS%\find.exe" /i "HiLanguageOL.exe" >nul
if not errorlevel 1 (
    echo [提示] 检测到 HiLanguageOL 正在运行，打包前需要先关闭它。
    if defined AUTO (
        "%SYS%\taskkill.exe" /f /im HiLanguageOL.exe >nul 2>nul
        echo        已自动关闭。
    ) else (
        "%SYS%\choice.exe" /c YN /n /m "        现在关闭它吗？[Y=关闭 / N=取消] "
        if errorlevel 2 ( echo         已取消。& goto FAIL )
        "%SYS%\taskkill.exe" /f /im HiLanguageOL.exe >nul 2>nul
    )
    "%SYS%\timeout.exe" /t 2 /nobreak >nul
)

if not exist ".venv\Scripts\pyinstaller.exe" (
    echo [提示] 未安装 PyInstaller，正在安装...
    "%PY%" -m pip install "pyinstaller>=6.0"
    if errorlevel 1 ( echo [错误] PyInstaller 安装失败 & goto FAIL )
)

set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [警告] 没有找到 Inno Setup 的 ISCC.exe，将只打包 exe、跳过安装包。
    echo        如需生成安装包，请安装 Inno Setup 6： https://jrsoftware.org/isdl.php
)

rem ---------- 2. 版本号（回车 = 自动 +1，也可手填 2.0.0）----------
echo.
"%PY%" tools\bump_version.py --show
set "NEXTVER="
for /f "delims=" %%v in ('"%PY%" tools\bump_version.py --next 2^>nul') do set "NEXTVER=%%v"
if defined AUTO goto VER_DONE
if defined NEXTVER (
    set /p NEWVER=       新版本号（直接回车 = !NEXTVER!）:
) else (
    set /p NEWVER=       新版本号（如 1.1.0）:
)
if not defined NEWVER set "NEWVER=!NEXTVER!"
if not defined NEWVER ( echo [错误] 读不到当前版本号，请检查 installer.iss & goto FAIL )
"%PY%" tools\bump_version.py "!NEWVER!"
if errorlevel 1 ( echo [错误] 版本号不合法，已中止。 & goto FAIL )
:VER_DONE
rem ---------- 3. 打包 exe（--log-level WARN 让输出更干净）----------
echo.
echo [1/3] PyInstaller 打包 exe ...
"%PY%" -m PyInstaller HiLanguageOL.spec --noconfirm --log-level WARN
if errorlevel 1 ( echo [错误] exe 打包失败 & goto FAIL )
if not exist "dist\HiLanguageOL\HiLanguageOL.exe" ( echo [错误] 没有生成 dist\HiLanguageOL\HiLanguageOL.exe & goto FAIL )

rem ---------- 4. 生成安装包 ----------
echo.
if not defined ISCC goto SKIP_SETUP
echo [2/3] Inno Setup 生成安装包 ...
"%ISCC%" installer.iss
if errorlevel 1 ( echo [错误] 安装包编译失败 & goto FAIL )
goto SECRET

:SKIP_SETUP
echo [2/3] 已跳过安装包（未找到 Inno Setup）

rem ---------- 5. 安检 ----------
:SECRET
echo.
echo [3/3] 安检：扫描发布产物里的敏感信息 ...
"%PY%" tools\check_package.py
if errorlevel 1 ( echo [错误] 安检未通过，禁止发布！ & goto FAIL )

echo.
echo ============================================================
echo   打包完成
echo     exe    : dist\HiLanguageOL\HiLanguageOL.exe
if defined ISCC echo     安装包 : installer\HiLanguageOL-Setup-*.exe
echo   提示：升级安装不会清除 %%APPDATA%%\HiLanguageOL 里的配置与录音
echo ============================================================
if not defined AUTO (
    "%SYS%\choice.exe" /c YN /n /m "  现在打开输出目录吗？[Y/N] "
    if not errorlevel 2 start "" explorer "%~dp0dist\HiLanguageOL"
    echo.
    pause
)
exit /b 0

:FAIL
echo.
echo [失败] 打包未完成，请根据上面的提示处理后重试。
if not defined AUTO pause
exit /b 1
