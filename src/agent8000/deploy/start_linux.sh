#!/usr/bin/env bash
# ===== 高数作业助手 启动脚本 (Linux) =====
# 用法: bash deploy/start_linux.sh   (前台验证)；生产请用 systemd/supervisor 守护。
set -euo pipefail

WORKBENCH_DIR="${WORKBENCH_DIR:-/opt/gaoshu/高数教材答案}"
AGENT_DIR="${AGENT_DIR:-/opt/gaoshu/高数作业助手}"
WB_LAUNCHER="${WB_LAUNCHER:-/opt/gaoshu/run_workbench_8014.py}"
export WORKBENCH_DB="${WORKBENCH_DB:-$WORKBENCH_DIR/api.workbench.db}"
export IMAGE_ROOT="${IMAGE_ROOT:-/opt/gaoshu/extract_img}"
VENV_PY="${VENV_PY:-/opt/gaoshu/venv/bin/python}"
MANAGED_PY="${MANAGED_PY:-/opt/gaoshu/venv/bin/python}"

echo "[*] 8014 工作台 -> $WB_LAUNCHER"
"$MANAGED_PY" "$WB_LAUNCHER" &
WB_PID=$!

echo "[*] 8000 智能体 -> $AGENT_DIR"
cd "$AGENT_DIR"
"$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 &
AGENT_PID=$!

echo "[ok] 已启动 workbench_pid=$WB_PID agent_pid=$AGENT_PID"
echo "     健康检查: curl http://127.0.0.1:8000/api/agent/capabilities"
echo "               curl http://127.0.0.1:8014/api/health"
trap "kill $WB_PID $AGENT_PID 2>/dev/null" EXIT INT TERM
wait
