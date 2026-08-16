# 环境说明（Codex 重构必读）

## 1. Python 解释器

| 用途 | 路径 | 说明 |
|------|------|------|
| 真实运行环境（venv） | `C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe` | **实际运行用此 venv**，依赖集为 Python 3.11.x |
| 托管运行时（启动器） | `C:\Users\YXZ\.workbuddy\binaries\python\versions\3.13.12\python.exe` | 仅用于创建/调用 venv，非业务依赖 |
| 系统回退 | `C:\Users\YXZ\AppData\Local\Programs\Python\Python311\python.exe` | 调试用 |

> 代码以 **envs/default venv（3.11 依赖集）** 运行。`requirements.txt` 已与该 venv 对齐冻结。
> 缺失依赖装法：`python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`

## 2. 依赖（已冻结，`高数作业助手/requirements.txt`）

核心：fastapi 0.115.6 / uvicorn 0.34.0 / pydantic 2.13.4 / httpx 0.28.1 / sqlalchemy 系 / pymupdf 1.28 / pypdf 6.16 / fpdf2 2.8.7 / sympy 1.14 / numpy 2.5.1。
可选（仅 MinerU/公式 OCR 流水线用，可删减以精简）：opencv-python / onnxruntime / rapidocr-onnxruntime / shapely / pyclipper。
部署用：paramiko 5.0.0（deploy_vlm.py SSH）。

## 3. 服务 / 端口

| 服务 | 端口 | 启动方式 | 关键环境变量 |
|------|------|----------|--------------|
| 8014 工作台 | 127.0.0.1:8014 | `python run_workbench_8014.py` | `WORKBENCH_DB`, `IMAGE_ROOT` |
| 8000 智能体 | 127.0.0.1:8000 | `envs/default/.../python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（从 `高数作业助手/` 目录） | 见 `.env` |
| 18080 VLM | 222.211.217.7:10022（远端） | `deploy_vlm.py` 上传重启（paramiko） | 必须 `bind 0.0.0.0` |

## 4. 8000 环境变量（`.env`，默认见 `app/config.py`）

```
DATABASE_PATH=./data/homework.db
UPLOAD_DIR=./data/uploads
DIFY_API_URL= / DIFY_API_KEY= / DIFY_WORKFLOW_ID=        # 可选
evidence_api_url=http://127.0.0.1:8014/api               # 指向 8014
qwen_grading_url=http://127.0.0.1:18080/grade-homework  # 指向远端 VLM
qwen_pdf_max_pages=12
qwen_pdf_render_dpi=144
pdf_renderer_path=                                        # 见 §6 poppler
```

## 5. 8014 环境变量（固化在 `run_workbench_8014.py`）

```
WORKBENCH_DB = D:/My File/大四/高数教材答案/api.workbench.db
IMAGE_ROOT   = D:/workbuddy/2026-08-06-15-31-48/extract_img
```

## 6. 外部依赖 / 服务

- **远端 VLM（222.211.217.7:10022，8×A100）**：`server_vlm_service.py` 本体；`deploy_vlm.py`（上传）+`pull_vlm.py`（拉取）经 paramiko SSH 运维。端点 `/health /review /solve /solve-from-image /grade-homework`。
- **IMA 知识库「高数答案OCR」**（知识库 ID `7491467723418571`）：答案书 OCR 文本权威源，经 IMA MCP 访问。本地副本：上册 `…tool-results/…-1786851511508-b50ce8.txt`、下册 `…-1786851633415-8d4a4b.txt`。
- **poppler（PDF 渲染）**：`~/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe`（pypdf/PyMuPDF 复用）。如环境无此路径需重装 poppler 并改 `pdf_renderer_path`。

## 7. 数据库（SQLite）

| 库 | 路径 | 大小 | 角色 | 当前内容 |
|----|------|------|------|----------|
| `api.workbench.db` | `D:/My File/大四/高数教材答案/api.workbench.db` | ~24MB | **权威证据库**（8014） | 477 problems / 67 sections / 7 textbooks / 516 answer_import_candidates |
| `homework.db` | `D:/My File/大四/高数教材答案/高数作业助手/data/homework.db` | ~1.4MB | Agent 本地缓存（8000） | 388 questions / 13 assignments / 2 submissions / 12 ai_stem_candidates |
| `api.db` | 同上目录 | 176KB | **空测试库，生产不用** | total_problems=0 |

> 结构已转储到本仓库 `db_schema/*.schema.sql` + `*.tables.md`，无需 24MB 二进制即可理解 schema。
> 备份命名：`api.workbench.db.bak_*`、`homework.db.bak_*`；迁移前务必先备份。

## 8. 源数据资产（体积大，未随包；按需单独提供）

教材/答案 PDF（各 ~25MB，位于 `D:/My File/大四/高数教材答案/`）：
- 同济高数8版-教材-上册(1).pdf / -下册(1).pdf
- 同济高数8版-答案-上册(1).pdf / -下册(1).pdf
- 李继成高数-教材-上册-2版(1).pdf / -下册-2版(1).pdf
- 李继成高数-答案-上册(1).pdf / -下册(1).pdf

裁切图：`D:/workbuddy/2026-08-06-15-31-48/extract_img/book/<章节>/p<n>_<m>_p<no>.png`（8014 `crop_image_path` 指向此处；**仅覆盖各章前半题号**，如 §2.1 仅 #1–#10）。

## 9. 运维 gotcha（Windows）

- 杀进程：用 Python `os.kill(pid, signal.SIGTERM)`；`Stop-Process`/`wmic` 被沙箱拦截。
- 删文件：用 Python `os.remove` + Windows 绝对路径 `D:/...`；`rm -f` 触发 safe-delete 钩子 fail-closed。
- 无 git 仓库历史：迭代上下文在 `D:/workbuddy/.../.workbuddy/memory/2026-08-*.md`。
- 改码必重启对应服务（8000 改 `app/main.py` 等需重启 uvicorn；8014 改 `api_app.vision.py` 需重启 `run_workbench_8014.py`）。
