@echo off
REM ===== 高数作业助手 生产启动脚本 (Windows) =====
REM 分别拉起 8014 工作台与 8000 智能体；VLM 在远程，无需本机启动。
setlocal
set WORKBENCH_DIR=D:\My File\大四\高数教材答案
set AGENT_DIR=%WORKBENCH_DIR%\高数作业助手
set WB_LAUNCHER=D:\workbuddy\2026-08-06-15-31-48\run_workbench_8014.py
set WORKBENCH_DB=%WORKBENCH_DIR%\api.workbench.db
set IMAGE_ROOT=D:\workbuddy\2026-08-06-15-31-48\extract_img
set VENV_PY=C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe

if not exist "%WB_LAUNCHER%" ( echo [错误] 找不到 8014 启动器: %WB_LAUNCHER% & pause & exit /b 1 )
if not exist "%WORKBENCH_DB%" ( echo [错误] 找不到 WORKBENCH_DB: %WORKBENCH_DB% & pause & exit /b 1 )

REM 1) 8014 工作台（必须用 run_workbench_8014.py；固化 WORKBENCH_DB / IMAGE_ROOT，否则裁切图 404）
REM 注意：必须用 venv python（%VENV_PY%），managed 3.13 裸环境缺 fastapi 会启动失败。
start "workbench-8014" %VENV_PY% %WB_LAUNCHER%

REM 2) 8000 智能体
cd /d %AGENT_DIR%
start "agent-8000" %VENV_PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

echo.
echo 已启动：工作台 8014 + 智能体 8000
echo VLM 在远程 222.211.217.7:18080（须网络可达；远程须 bind 0.0.0.0）
echo 健康检查：
echo   curl http://127.0.0.1:8000/api/agent/capabilities
echo   curl http://127.0.0.1:8014/api/health
echo 关闭：任务管理器结束 workbench-8014 / agent-8000 窗口对应的 python 进程。
pause
