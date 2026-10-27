@echo off
chcp 936 >nul
setlocal EnableExtensions

rem ============================================================
rem  NVIDIA vGPU nvxdapix.dll 补丁工具 - 一键打包脚本
rem  功能: 用 PyInstaller 把 patcher_gui.py 打包成单文件 exe
rem        输出到 release\NVIDIA_GRID_Driver_Patcher.exe
rem        并附带 gridd-unlock-patcher.exe
rem
rem  【Python 解释器】固定使用下方 PYTHON 变量指定的 venv (Python 3.x)。
rem    本机若装有 Python 2.7 也不影响, 本脚本不走 PATH 探测。
rem    如需换解释器, 改下面 PYTHON 一行, 或运行前:
rem        set PYTHON=D:\path\to\python3xx.exe
rem ============================================================

rem ---- 固定使用的 Python (venv) ----
rem ---- 自动检测 Python (要求 3.x) ----
if defined PYTHON goto :python_ok
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    goto :python_ok
)
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py -3"
    goto :python_ok
)
echo [错误] 未找到 Python 3.x。请安装 Python 3 或运行前设置:
echo        set PYTHON=D:\path\to\python3xx.exe
exit /b 1
:python_ok

cd /d "%~dp0"
echo ============================================================
echo   工程目录: %CD%
echo   使用 Python: %PYTHON%
echo ============================================================

rem ---------- 0. Python 检查 ----------
if not exist "%PYTHON%" (
    echo [错误] 未找到 Python: %PYTHON%
    echo        请修改本脚本顶部的 PYTHON 变量, 指向一个可用的 Python 3.x。
    pause
    exit /b 1
)
"%PYTHON%" -c "import sys; assert sys.version_info[0]==3" >nul 2>&1
if errorlevel 1 (
    echo [错误] %PYTHON% 不是 Python 3.x, 无法打包。
    pause
    exit /b 1
)
"%PYTHON%" --version

rem ---------- 1. 依赖检查 ----------
"%PYTHON%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [错误] 选定的 Python 缺少 tkinter, 无法打包 GUI 程序。
    pause
    exit /b 1
)
"%PYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [信息] 选定的 Python 缺少 PyInstaller, 正在从清华镜像安装...
    "%PYTHON%" -m pip install --upgrade pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败, 请手动安装后重试。
        pause
        exit /b 1
    )
)

if not exist "gridd-unlock-patcher.exe" (
    echo [错误] 未找到 gridd-unlock-patcher.exe, 请将其放到工程根目录。
    pause
    exit /b 1
)
if not exist "patcher_gui.py" (
    echo [错误] 未找到 patcher_gui.py。
    pause
    exit /b 1
)

echo [信息] 依赖检查通过, 开始打包...
echo.

rem ---------- 2. 清理旧的中间文件 ----------
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "__pycache__" rmdir /s /q "__pycache__"
if exist "*.spec" del /q "*.spec"
if not exist "release" mkdir "release"
if exist "release\NVIDIA_GRID_Driver_Patcher.exe" del /q "release\NVIDIA_GRID_Driver_Patcher.exe"
if exist "release\gridd-unlock-patcher.exe" del /q "release\gridd-unlock-patcher.exe"

rem ---------- 3. 执行打包 (相对路径, 含管理员权限 manifest) ----------
echo [信息] 开始 PyInstaller 打包 (带 requireAdministrator manifest)...
"%PYTHON%" -m PyInstaller --onefile --windowed --uac-admin ^
    --name "NVIDIA_GRID_Driver_Patcher" ^
    --add-data "gridd-unlock-patcher.exe;." ^
    --distpath "dist" ^
    --workpath "build" ^
    --specpath "." ^
    patcher_gui.py
if errorlevel 1 (
    echo.
    echo [错误] PyInstaller 打包失败, 请查看上方日志。
    pause
    exit /b 1
)

rem ---------- 4. 产物移动到 release 目录 ----------
if not exist "dist\NVIDIA_GRID_Driver_Patcher.exe" (
    echo [错误] 未生成 dist\NVIDIA_GRID_Driver_Patcher.exe
    pause
    exit /b 1
)
move /y "dist\NVIDIA_GRID_Driver_Patcher.exe" "release\NVIDIA_GRID_Driver_Patcher.exe" >nul
copy /y "gridd-unlock-patcher.exe" "release\gridd-unlock-patcher.exe" >nul

rem ---------- 5. 清理中间文件 ----------
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "__pycache__" rmdir /s /q "__pycache__"
if exist "*.spec" del /q "*.spec"

echo.
echo ============================================================
echo   打包完成!
echo   发版产物 (release 目录):
echo ============================================================
dir "release"
echo.
pause
endlocal
