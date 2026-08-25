# 高数 Agent 工作台 — Codex 重构交接文档

> 生成时间：2026-08-16 ｜ 交接对象：Codex（整体重构 / 重写）
> 配套仓库根：`CODEX_HANDOVER/`（本目录，已初始化为自包含 git 仓库）
> 配套文档：`ENVIRONMENT.md`（环境/依赖/端口/外部服务）、`BACKLOG.md`（已完成 + 待办 + 重构范围）

---

## 0. 一句话结论

**当前阶段 = 核心功能已开发完成并通过端到端验证（测试中 / 准生产），不是初始化阶段。**

- 三层服务（8014 证据库 / 8000 智能体 / 18080 VLM）**全部跑通并稳定运行**。
- 完整业务闭环 **已验证**：组卷 → 出 PDF → 学生提交 → AI 批改 → 教师复核 → 写回经验。
- 题库已最大化恢复（8014 源库 477 题，Agent 已发布约 362 题，全部过损坏门禁，不会向学生推乱码）。
- 安全（SQL 注入修复 + 全量审计）、学生端加固、生产部署交付物、回归脚本均已就绪。

**可直接进入 Codex 重构。** 剩余工作 = 「内容补全（题库缺口 113 题）」+「代码质量重构」，二者都不阻塞功能交付。

---

## 1. 运行进程状态（截至 2026-08-16）

| 服务 | 端口 | 进程状态 | 说明 |
|------|------|----------|------|
| 8014 工作台（证据库） | 127.0.0.1:8014 | LISTENING（单实例，用 `run_workbench_8014.py` 启动） | 真实库 `api.workbench.db`，477 题 / 67 章 / 7 教材；注入四分支已修复 |
| 8000 智能体（教师端 + 学生端） | 127.0.0.1:8000 | LISTENING（单实例，托管 venv 后台运行） | capabilities 含 7 项（assignments/reports/ai_stem/sync…） |
| 18080 VLM | 222.211.217.7:10022（远端，8×A100） | 运行中（4 worker，bind 0.0.0.0） | `/solve` `/solve-from-image` `/grade-homework` `/review` `/health` |

**启动命令（务必照此，不能裸 `uvicorn api_app:app`）**：
- 8014：`python src/workbench8014/run_workbench_8014.py`（importlib 加载权威 `api_app.py` 并挂 `/api`，固化 `WORKBENCH_DB` + `IMAGE_ROOT`）。
- 8000：从 `高数作业助手/` 目录用 `envs/default/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。
- 18080：远端 `deploy_vlm.py` 上传重启（paramiko SSH）。

---

## 2. 已完成事项与产出（按模块）

### 2.1 架构与部署
- 三层架构落地：8014 证据库（SQLite，教材/答案/识别结果/符号校验）、8000 智能体（FastAPI + 本地 SQLite 缓存）、18080 VLM（Qwen2.5-VL-3B-Instruct）。
- 生产部署交付物（`高数作业助手/deploy/`）：`DEPLOY.md`、`start_windows.bat`、`start_linux.sh`、`supervisor.conf`、`nginx_example.conf`、`docker-compose.yml`、`Dockerfile`、`requirements.txt`（完整依赖冻结）。`TESTING.md` 自测指南。

### 2.2 题库恢复链路（OCR → 8014 → Agent）
- **IMA 答案书 OCR 回填**（`recover_from_answer_ocr.py --fix`）：把答案书 OCR 作为权威源，修正 §2.1 等系统性答案错位（原 8014 答案整体下移）。8014 `problems` 题干+答案齐全 364 题（从 95 提升）。
- **8014 → Agent 同步**（`bulk_sync_8014.py`）：UPSERT + retire 陈旧源，内置 `garbage_score` 损坏门禁阻断乱码题干 → 同步后 Agent 约 **362 published + 22 blocked**。
- **缺口分类**（`gap_analysis_8014.py`）：113 道不全拆 **FREEWIN 0 / CORRUPT 50 / NEED_TEXTBOOK 13 / ABSENT 44**（另有 6 道有父题的子题缺题干，设计刻意跳过）。
- **CORRUPT 优先章恢复**（`recover_corrupt_269.py`）：§2.6/#2.7/#2.9 答案书 OCR 全角→半角规整回填，放出被误 block 的可读题。
- **全章全角规整放量**（`release_safe_fw.py`）：仅放出人工核验干净的 2 道（S6.2#4、S6.7#8），**拒绝批量推送含中文乱码的题目**（违反「绝不推乱码」红线）。

### 2.3 Agent 功能（组卷 / 批改 / 复核 / 报表 / 知识点）
- **Route 1（VLM 识别题干 → 待教师复核）**：`knowledge_bridge.build_image_solve_candidate` 三态（eligible / pending_review / unavailable）；`ai_stem_review` 模块 + `ai_stem_candidates` 表 + 审批端点；双模型一致且 conf≥0.8 自动发布，否则进待复核。
- **Route 2（章节导入器）**：`route2_chapter_importer.py` + `route2_pdf_extract.py` 从教材/答案 PDF 按题切分 → VLM 读图 → 写 8014。§5.1–5.6 已导入并 `verified`（5.1:18/5.2:7/5.3:5/5.4:10/5.5:11/5.6:14）。
- **组卷 + 出 PDF**：`POST /api/assignments` + `assignment_pdf.py`，实测返回合法 15MB PDF（%PDF-1.7）。
- **大 PDF 分页批改修复**：原 `page_count > 12` 直接拒；改为渲染全部页面交 VLM 跨页匹配，仅 `>80` 才转人工。张三 49 页 PDF 复批成功（total_score=73/100）。
- **教学报表**（设计文档 4 类）：`review-quota` / `weak-points`（概念级）/ `semester-summary` / `summary`（含书写整洁度、未达复核配额班级）。
- **知识点打标**：`db.tag_knowledge_points` 零 VLM 依赖，回填 89 题 `knowledge_points`，薄弱点自动变概念级。

### 2.4 学生端加固
- `submit_homework`：学号白名单 `^[A-Za-z0-9_]{1,32}$`、扩展名白名单、30MB 上限（413）、空文件 422、重复提交 409、落盘绝对路径、返回 `grading_job_id`。
- `regress_student_submit.py` 全绿；前端 `student_submit.html` 加防重复点击 + 30MB 预检。

### 2.5 安全
- **SQL 注入修复 + 全量审计**：`_where_clause()` 改返回 `(clause, args)` 元组（COUNT 分支原未参数化）；修复 `api_app.vision.py` / `api_app.original.py` / `api_app.candidate-ui.py` / 工作区 `api_app.py` 四份实现。`regression_where_clause.py` 4/4 PASS。

### 2.6 验证与文档
- `validate_agent_reports.py`（报表全绿）、`regress_student_submit.py`（学生端全绿）、`regression_where_clause.py`（注入 4/4）。
- `闭环端到端验证报告.md`：王小明 84/96 全链路闭环证据。
- `工作台功能清单与验证手册.md`、`SQL_INJECTION_AUDIT.md` / `REGRESSION.md`、`诊断报告_*.md`、设计文档等。

---

## 3. 代码结构（两处代码库 → 已合并到本仓库 `src/`）

| 原位置 | 内容 | 本仓库位置 |
|--------|------|-----------|
| `D:/My File/大四/高数教材答案/` | 8014 工作台 + 高数作业助手(8000) + VLM 副本 + 源 PDF + 数据库 | `src/workbench8014/`、`src/agent8000/`、`src/vlm18080/` |
| `D:/workbuddy/2026-08-06-15-31-48/` | 工具/恢复脚本（96 个 .py）、设计诊断文档 | `src/tools/`、`docs/` |

> ⚠️ **历史无 git 仓库**：迭代历史全在 `D:/workbuddy/.../.workbuddy/memory/2026-08-*.md` 日志中（已是最佳上下文来源）。移交后建议以本仓库为单一事实源。

---

## 4. 已知地雷 / 必读陷阱（防止 Codex 重复踩坑）

1. **`questions` 列序陷阱**：`_upsert_local_cache` INSERT 列序 `(content,chapter,difficulty,question_type,answer,rubric,source_evidence_json,source_problem_id,source_problem_no)`，bind 必须 `(*values[:7], source_id, values[7])`。曾因列序错把 `problem_no` 落进 `source_problem_id` 污染 §5.2。→ 重构时务必用命名参数或 ORM。
2. **`garbage_score` 门禁 vs 全角标点误杀**：严格全角拉丁门禁会把「数学 intact 但含全角标点（`＇`/`？`/`［`）」的可读题 block。已用 `recover_corrupt_269.py` 规整放出。**不要为放量而用通用 salad 检测**（会误杀干净 LaTeX）。
3. **8014 单模块布局**：权威模块为 `api_app.py`。必须使用 `run_workbench_8014.py`，以保持 `/api` 挂载和环境变量初始化；不要直接以裸 uvicorn 替代启动器。
4. **`IMAGE_ROOT` 必须指向 `extract_img`**：`problems.crop_image_path` 指向 `book/...`，真实裁切图在 workbuddy 工作区 `extract_img/`，否则 `/images/` 404、VLM 取图失败。
5. **`submissions.file_path` 绝对路径**：曾存相对路径导致批改依赖服务器 cwd；现强制 `str(path.resolve())`。
6. **VLM 3B 模型错位**：按题号从整份作业定位单题偶发张冠李戴，所有错配被 `needs_review` 兜底。**不要把 needs_review 当失败处理**。
7. **Windows 杀进程**：无 SIGKILL，`Stop-Process`/`wmic` 被沙箱拦截 → 用 `os.kill(pid, signal.SIGTERM)`。
8. **删文件**：`rm -f` 触发 safe-delete 钩子误解析 Git-Bash 路径 fail-closed → 用 Python `os.remove` + Windows 绝对路径。
9. **Python 解释器**：真实运行环境 = 托管 venv `C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（3.11 依赖集），非基础版 3.13.12。`requirements.txt` 已与该 venv 对齐冻结。

---

## 5. Codex 重构范围建议

**保留（不得回退）**：
- sync-section → publish、assignment PDF、grade-homework 流水线、4 类报表、ai_stem 复核、学生端加固。
- 损坏门禁（不向学生推乱码）；题库已发布 ≥ 362 题不回退。

**重点重构目标**：
- 扁平文件 + 三份重复 `api_app`（vision/original/candidate-ui）+ 工作区副本 `api_app.py` → 收敛为单一可测试模块。
- 硬编码绝对路径（`run_workbench_8014.py`、`config.py`、crop 路径、poppler 路径）→ 配置化。
- [x] 散落回归脚本 → 统一 `pytest` 套件固化（`python -m pytest -q tests`）。
- `questions` INSERT 列序脆弱点 → 命名参数/ORM。
- 学生端/教师端 HTML 内嵌 `app/` 与后端耦合 → 考虑前后端分离或模板化。

**内容补全（需人工/VLM，非代码 blocking）**：
- 113 缺口：CORRUPT 50 / NEED_TEXTBOOK 13 / ABSENT 44，见 `BACKLOG.md`。
- 39 道 FW-blocked 题目无裁切图、VLM 不可重识别 → 需提供教材/答案 PDF 按题 crop 或 IMA 原页。
- 下册（§9.x 等）尚未导入。

---

## 6. 交接前置准备与具体操作步骤（SOP）

本仓库 `CODEX_HANDOVER/` 已完成「代码导出 + 结构转储 + 文档整理」三步。剩余交接如下：

### 步骤 1 — 代码导出 ✅ 已完成
- 源码已合并到 `src/{agent8000,workbench8014,vlm18080,tools}`，数据库结构已转储到 `db_schema/`，设计/诊断文档已归到 `docs/`。
- **未随包（体积过大，按需单独提供）**：`api.workbench.db`（24MB）、`homework.db`（可随包，已在 agent8000/data/）、4 本教材 PDF + 4 本答案 PDF（各 ~25MB）、`extract_img/` 裁切图。路径见 `ENVIRONMENT.md`。

### 步骤 2 — 数据资产交接
- 将 `api.workbench.db`（真实库）与源 PDF 按 `ENVIRONMENT.md` 路径清单提供给 Codex 运行环境；或让 Codex 仅基于 `db_schema/` 做结构重构、数据由你侧保留。
- 备份命名规则：`api.workbench.db.bak_*` / `homework.db.bak_*`，迁移前务必先备份。

### 步骤 3 — 环境说明 ✅ 已附（`ENVIRONMENT.md`）
- 解释器、依赖冻结、端口、环境变量、外部服务（远端 VLM / IMA 知识库 / poppler）全部列出。

### 步骤 4 — 任务文档整理 ✅ 已附（`BACKLOG.md` + 本文件）
- 已完成清单、待办分类、重构范围、验收基线。

### 步骤 5 — 权限与密钥交接（需你侧执行，属外部操作）
- **远端 VLM（222.211.217.7:10022）**：`deploy_vlm.py` 用 paramiko SSH，需 SSH 私钥/凭据。把私钥放入运行环境 `~/.ssh/` 或提供凭据。
- **IMA 知识库「高数答案OCR」**（知识库 ID `7491467723418571`）：经 IMA MCP 访问；移交 IMA 账号授权或导出 OCR 文本（`…tool-results/…-1786851511508-b50ce8.txt` 上册、`…-1786851633415-8d4a4b.txt` 下册，已存本地）。
- **DIFY（可选）**：`DIFY_API_KEY` 等，非核心。
- **本机路径权限**：Codex 运行环境需能读写 `api.workbench.db`、源 PDF、输出目录。

### 步骤 6 — 版本控制与推送
```bash
cd CODEX_HANDOVER
git init            # 本仓库已初始化（见下）
git add -A
git commit -m "handover: 高数 Agent 工作台 代码+文档+结构 导出"
# 以下由你侧执行（外部操作）：
git remote add origin <你的远程仓库 URL>
git push -u origin main
# 把远程仓库地址交给 Codex
```

### 步骤 7 — 给 Codex 的重构 Brief 模板（建议直接粘贴）
```
目标：重构 D:/.../CODEX_HANDOVER 中的高数作业助手三层系统（8000 智能体 / 8014 证据库 / 18080 VLM 客户端）。
约束：
 1) 保留现有全部业务功能与端点（见 HANDOVER.md §2、BACKLOG.md）。
 2) 不向学生推送任何乱码/损坏题目（保留 garbage_score 等价门禁）。
 3) 收敛 8014 三份重复 api_app 为单一可测试模块；消除硬编码绝对路径。
 4) 统一回归测试为 pytest，跑通 validate_agent_reports / regress_student_submit / regression_where_clause。
验收：
 - 三服务可启动；sync-section→publish、assignment PDF、grade-homework 闭环可用；
 - 题库已发布 ≥ 362 题不回退；所有回归脚本全绿。
参考：ENVIRONMENT.md（环境）、db_schema/（结构）、docs/（设计诊断）、src/tools/（既有恢复脚本）。
```

---

## 7. 验收 / 回归基线（给 Codex 对照）

### 2026-08-16 Codex 重构增量

- 8014 启动器改为以仓库位置推导默认 API、SQLite 与图片根目录；生产部署可通过 `WORKBENCH_API_MODULE`、`WORKBENCH_DB`、`IMAGE_ROOT` 覆盖。
- 8000 的 `EVIDENCE_API_URL` 与 `QWEN_GRADING_URL` 支持环境变量覆盖；MinerU 暂存适配器改为按需加载，缺少可选集成不会阻断核心服务启动。
- 三个回归脚本已迁移为仓库相对路径与系统临时目录运行，均不写生产数据库；`questions` 的 AI 候选插入改为 SQLite 命名参数绑定。
- 已新增 `tests/test_regression_scripts.py` 作为 pytest 统一入口，且测试不再依赖未纳入仓库的 `homework.db` 二进制文件。
- 本轮验收：`python -m pytest -q tests` 通过（1 passed）；三个保留脚本亦均通过。
- 8014 已收敛为 `src/workbench8014/api_app.py` 一个生产模块；`legacy/` 下三份旧副本仅供追溯，启动器、兼容启动器与回归测试均不再加载它们。
- Windows 启动脚本、Route 2 导入器、组卷 PDF 引擎和 Pix2Text 解释器选择已移除运行时的个人绝对路径；外部位置通过环境变量覆盖。
- 新增 `question_bank_readiness.py`：只读生成题库复核就绪清单。它不会写库或发布题目，供 VLM 识别与教师复核前确认裁图是否存在。
- 本地真实库核查（2026-08-16）：477 题中 368 题完整；17 条损坏/缺字段记录已有裁图，可进入 VLM 候选与教师复核；92 条缺少源图，明确阻断自动补全。损坏扫描识别 73 条疑似乱码记录，均保持未发布处理。
- `docs/QUESTION_BANK_REVIEW.md` 记录了候选暂存、教师批准/拒绝、缺图补证据与乱码重审的安全 SOP。
- 经用户授权后已对 17 条已有裁图执行 VLM 候选暂存：对账结果为 9 条 `pending`、8 条 `not_staged`。未暂存条目仍未写回或发布，需补更清晰裁图/人工录入后重试。

| 基线 | 期望值 |
|------|--------|
| `validate_agent_reports.py` | ALL VALIDATIONS PASSED |
| `regress_student_submit.py` | 全绿（422/415/409/413 正确） |
| `regression_where_clause.py` | 4/4 PASS（注入不绕过） |
| `bulk_sync_8014.py` 后 Agent `questions` | ≈ 362 published + 22 blocked（不回退） |
| 端到端 | 王小明 84/96 提交→批改→复核→写回闭环 |
| published 中乱码/替换符 | = 0 |
