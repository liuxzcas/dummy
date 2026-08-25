@echo off
cd /d %~dp0
rem ---- git-bash 检测(terminal 工具依赖 bash 执行)----
rem 已手动设置 DUMMY_BASH_PATH 时跳过检测
if not "%DUMMY_BASH_PATH%"=="" goto :run
rem 常见安装位置(低优先级先检测,高优先级后覆盖)
if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" set "DUMMY_BASH_PATH=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
if exist "C:\Program Files (x86)\Git\bin\bash.exe" set "DUMMY_BASH_PATH=C:\Program Files (x86)\Git\bin\bash.exe"
if exist "C:\Program Files\Git\bin\bash.exe" set "DUMMY_BASH_PATH=C:\Program Files\Git\bin\bash.exe"
:run
if "%DUMMY_BASH_PATH%"=="" echo [提示] 未检测到 git-bash,terminal 工具将回退 cmd(可安装 Git 或设置 DUMMY_BASH_PATH)
python main.py
