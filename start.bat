@echo off
rem HiLanguageOL 启动脚本：优先使用项目自带虚拟环境 .venv（无控制台启动）
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py
    exit /b 0
)

echo [提示] 未找到项目虚拟环境 .venv，尝试用系统的 Python 启动...
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw main.py
    exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw main.py
    exit /b 0
)

echo.
echo [错误] 没有找到可用的 Python 环境，程序无法启动。
echo        请先在本目录执行下面两条命令建立虚拟环境：
echo            python -m venv .venv
echo            .venv\Scripts\pip install -r requirements.txt
echo.
pause
exit /b 1
