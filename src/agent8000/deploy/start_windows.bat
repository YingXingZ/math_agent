@echo off
REM ===== 高数教材答案 Agent：Windows 单机试点启动器 =====
REM 从仓库位置推导路径；不要复制旧的 D:\workbuddy 绝对路径。
setlocal
set "DEPLOY_DIR=%~dp0"
for %%I in ("%DEPLOY_DIR%..\..\..") do set "REPO_ROOT=%%~fI"
set "AGENT_DIR=%REPO_ROOT%\src\agent8000"
set "WB_LAUNCHER=%REPO_ROOT%\src\workbench8014\run_workbench_8014.py"
set "WORKBENCH_DB=%REPO_ROOT%\api.workbench.db"
set "IMAGE_ROOT=%REPO_ROOT%\answer_source_previews"
set "OCR_REPAIR_IMAGE_ROOT=%REPO_ROOT%\answer_source_previews"
set "DATABASE_PATH=%AGENT_DIR%\data\homework.db"
set "EVIDENCE_API_URL=http://127.0.0.1:8014/api"
set "VENV_PY=C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not exist "%VENV_PY%" (echo [错误] 找不到 Python: %VENV_PY% & pause & exit /b 1)
if not exist "%WB_LAUNCHER%" (echo [错误] 找不到 8014 启动器: %WB_LAUNCHER% & pause & exit /b 1)
if not exist "%WORKBENCH_DB%" (echo [错误] 找不到 8014 数据库: %WORKBENCH_DB% & pause & exit /b 1)
if not exist "%DATABASE_PATH%" (echo [错误] 找不到 8000 数据库: %DATABASE_PATH% & pause & exit /b 1)

REM 8014 必须经启动器加载，不能裸起 uvicorn。
start "math-workbench-8014" cmd /k "set WORKBENCH_DB=%WORKBENCH_DB%&& set IMAGE_ROOT=%IMAGE_ROOT%&& set OCR_REPAIR_IMAGE_ROOT=%OCR_REPAIR_IMAGE_ROOT%&& cd /d %REPO_ROOT%&& %VENV_PY% %WB_LAUNCHER%"
start "math-agent-8000" cmd /k "set DATABASE_PATH=%DATABASE_PATH%&& set EVIDENCE_API_URL=%EVIDENCE_API_URL%&& cd /d %AGENT_DIR%&& %VENV_PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1"

echo.
echo 已请求启动 8014 工作台与 8000 智能体。请等待数秒后运行：
echo powershell -ExecutionPolicy Bypass -File "%DEPLOY_DIR%healthcheck_windows.ps1"
echo 学生/教师主入口：http://127.0.0.1:8000/
echo OCR 复核入口：http://127.0.0.1:8014/ocr-repair
pause
