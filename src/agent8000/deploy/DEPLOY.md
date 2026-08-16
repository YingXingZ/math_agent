# 高数作业助手 —— 生产化部署指南

## 1. 系统架构（三层）

| 层 | 端口 | 进程 / 启动方式 | 作用 |
|----|------|----------------|------|
| 工作台 (8014) | 8014 | `run_workbench_8014.py` | 教材/答案源库管理、AI 题干候选、MinerU 复核 |
| 智能体 (8000) | 8000 | `uvicorn app.main:app` | 组卷、学生提交、VLM 批改、报表、教师复核 |
| VLM 推理 (18080) | 18080 | 远程 `222.211.217.7:10022`（已部署） | Qwen2.5-VL 识别题干/批改定位 |

> 8000 与 8014 都跑在**同一台应用服务器**；VLM 在另一台 GPU 机器上，通过网络访问。
> 公网只暴露一个入口（nginx），反向代理到 8000（主）与 8014（管理台）。

## 2. 关键环境变量（务必固化，否则会静默失效）

| 变量 | 用于 | 取值 | 不设置的后果 |
|------|------|------|--------------|
| `WORKBENCH_DB` | 8014 | `D:\My File\大四\高数教材答案\api.workbench.db` | 8014 用空库 `api.db`，与 Agent 数据脱钩 |
| `IMAGE_ROOT` | 8014 | `D:\workbuddy\2026-08-06-15-31-48\extract_img` | `/images/` 全 404，VLM 取不到裁切图 |
| `DATABASE_PATH` | 8000 | `高数作业助手/data/homework.db` | 用进程 cwd 下的库，路径漂移 |
| （venv） | 8000 | `…\python\envs\default\Scripts\python.exe` | 依赖缺失无法启动 |

> ⚠️ **8014 不能用 `uvicorn api_app.vision:app` 直接起**——它是扁平文件，`run_workbench_8014.py`
> 已用 `importlib` 正确加载并固化上述两个环境变量。这是历史踩坑点。

## 3. 依赖安装（一次性）

项目自带完整依赖冻结 `requirements.txt`（与已验证运行环境一致）：

```bash
cd 高数作业助手
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

> 若只跑核心「组卷+批改+报表」，可从 `requirements.txt` 删去
> `opencv-python / onnxruntime / rapidocr-onnxruntime / sympy / python-pptx / xlsxwriter / openpyxl`
> 等仅被可选 MinerU/OCR 流水线使用的包以精简体积。

## 4. 启动方式

### 4.1 Windows（开发/单机生产）—— `deploy/start_windows.bat`
双击运行即可，分别拉起 8014 与 8000。脚本已固化 `WORKBENCH_DB` / `IMAGE_ROOT`。

### 4.2 Linux（推荐生产）—— `deploy/start_linux.sh` + 进程守护
```bash
chmod +x deploy/start_linux.sh
deploy/start_linux.sh          # 前台验证启动
# 生产用 systemd / supervisor 守护，见 deploy/supervisor.conf 或下方示例
```
systemd 单元示例（/etc/systemd/system/math-agent.service）：
```ini
[Unit]
Description=高数作业助手 8000
After=network.target
[Service]
WorkingDirectory=/opt/gaoshu/高数作业助手
Environment=DATABASE_PATH=/opt/gaoshu/高数作业助手/data/homework.db
ExecStart=/opt/gaoshu/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
User=math
[Install]
WantedBy=multi-user.target
```
（8014 同理，ExecStart 指向 `run_workbench_8014.py` 并带 `WORKBENCH_DB`/`IMAGE_ROOT` 环境变量。）

### 4.3 Docker（可选）—— `deploy/docker-compose.yml`
仅容器化 8000（依赖干净）；8014 与 VLM 作为既有服务通过 `extra_hosts` / 网络互通。
见文件内注释，按实际路径挂载 `data/` 与 `requirements.txt`。

## 5. 反向代理（nginx）—— `deploy/nginx_example.conf`
- `/` → 8000（学生/教师主入口）
- `/workbench/` → 8014（管理台，建议加 Basic Auth 或 IP 白名单）
- 关键：放通 `client_max_body_size 50m`（学生 PDF 上传）；`proxy_read_timeout` 调大（VLM 批改慢）。

## 6. 健康检查与运维

```bash
curl http://127.0.0.1:8000/api/agent/capabilities   # 智能体就绪
curl http://127.0.0.1:8014/api/health               # 工作台就绪
curl -m 5 http://222.211.217.7:18080/health         # VLM 就绪（远程）
```

- **日志**：uvicorn 输出到各自控制台/日志文件；8014 日志在 `run_workbench_8014.py` 同目录。
- **重启 8000**：`os.kill(pid, SIGTERM)`（Windows 无 SIGKILL；勿用 `Stop-Process`/被杀软拦截）。
- **VLM 重启**：远程执行 `bash /opt/math-vlm/start_vlm.sh`（已固化 orphan-safe 流程）。

## 7. 回滚
- 代码回滚：git checkout 到上一个稳定 commit，重启服务。
- 数据回滚：`data/homework.db` 与 `api.workbench.db` 每日备份（见 `.bak_*` 产物），
  异常时用备份覆盖即可，两库相互独立。

## 8. 安全建议（上线前）
1. 8014 管理台必须置于内网或加认证，绝不能直接公网暴露。
2. 学生提交端点已做：学号格式白名单（防路径穿越）、文件类型白名单、30MB 上限、
   空文件拦截、同作业重复提交 409 防护——生产仍建议 nginx 层再加限流（limit_req）。
3. 数据库连接为学生提交的 `file_path` 已统一为**绝对路径**（2026-08-16 迁移），
   不再依赖服务器 cwd，部署目录可移动。
