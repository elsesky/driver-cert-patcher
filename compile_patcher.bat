@echo off
chcp 65001 >nul
setlocal
echo === 编译补丁核心 (gridd-unlock-patcher.exe) ===
echo.

rem 查找 MinGW64 gcc
set "GCC="
if exist "D:\Cygwin\bin\bin\x86_64-w64-mingw32-gcc.exe" (
    set "GCC=D:\Cygwin\bin\bin\x86_64-w64-mingw32-gcc.exe"
) else (
    where x86_64-w64-mingw32-gcc >nul 2>&1
    if !errorlevel!==0 (
        set "GCC=x86_64-w64-mingw32-gcc"
    )
)

if "!GCC!"=="" (
    echo [错误] 未找到 MinGW64 交叉编译器 (x86_64-w64-mingw32-gcc)。
    echo        请安装 MinGW64 或设置 GCC 变量。
    exit /b 1
)

echo 使用编译器: %GCC%
cd /d %~dp0

%GCC% -O2 -Wall -o gridd-unlock-patcher.exe gridd-unlock-patcher-mini.c
if !errorlevel!==0 (
    echo [错误] 编译失败。
    exit /b 1
)

echo.
echo 编译成功: gridd-unlock-patcher.exe
for %%F in (gridd-unlock-patcher.exe) do echo   大小: %%~zF bytes
endlocal
