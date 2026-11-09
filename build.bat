@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] 创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo 创建失败：请先安装 Python 3.9+ 并加入 PATH
        pause
        exit /b 1
    )
)

echo [2/4] 安装依赖...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
".venv\Scripts\python.exe" -m pip install pyinstaller -q
if errorlevel 1 (
    echo 依赖安装失败
    pause
    exit /b 1
)

echo [3/4] 打包...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name HDU_Lib_AutoBook ^
    --icon "assets\icon.ico" ^
    --add-data "config;config" ^
    --add-data "assets;assets" ^
    --collect-all customtkinter ^
    --collect-submodules selenium ^
    app.py
if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

echo [4/4] 完成：dist\HDU_Lib_AutoBook.exe
pause
