# Codex 任务指令：高数 Agent 工作台 —— 改进与完善

> 这是给 **Codex（重构/重写执行方）** 的任务指令。配套压缩包 `CODEX_HANDOVER.zip` 已包含完整自包含仓库（代码 + 文档 + 真实库 + `.git`）。
> 你将本文件与压缩包一起交给 Codex 即可。Codex 解压后按本指令执行。

---

## 0. Codex 第一步：解包、读文档、跑通现状（务必先做）

1. 解压 `CODEX_HANDOVER.zip`，进入 `CODEX_HANDOVER/` 目录（已含 `.git`，可直接 `git push`）。
2. **按顺序读这三份文档**（均在仓库根）：
   - `HANDOVER.md` —— 必读 §0 结论 / §1 运行进程状态 / §4 已知地雷（9 条坑）/ §5 重构范围。
   - `ENVIRONMENT.md` —— 解释器、依赖冻结、端口、环境变量、外部服务（远端 VLM / IMA 知识库 / poppler）。
   - `BACKLOG.md` —— 已完成清单、待办分类、重构范围、验收基线。
3. **搭环境**（严格按 `ENVIRONMENT.md`）：
   - Python 真实运行环境是**托管 venv**（`C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，3.11 依赖集），`src/agent8000/requirements.txt` 已冻结，对齐安装。
   - 8014 必须用 `python src/workbench8014/run_workbench_8014.py` 启动（扁平文件布局，裸 `uvicorn api_app.vision:app` 会 No module）。
   - 8000 从 `src/agent8000/` 目录用该 venv 跑 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。
   - Windows 杀进程用 `os.kill(pid, signal.SIGTERM)`；`Stop-Process`/`wmic` 被沙箱拦截不可用。
4. **数据就位**：真实库已在 `src/workbench8014/api.workbench.db`；按 `ENVIRONMENT.md` 的 `WORKBENCH_DB`/`IMAGE_ROOT` 指向它即可（或保持默认）。源教材/答案 PDF、`extract_img/` 裁切图体积大未随包，**图像相关功能（VLM 取裁切图、Route 2 按 PDF 导入）在缺这些文件时先跳过，不要硬做**。
5. **先验证现状基线（确保你接手时是绿的）**：
   - `python src/tools/validate_agent_reports.py` → 全绿
   - `python src/tools/regress_student_submit.py` → 全绿
   - `python src/tools/regression_where_clause.py` → 4/4 PASS
   - 启动三服务，确认 `GET /api/agent/capabilities`(8000) / `/api/health`(8014) / `/health`(18080) 正常。

---

## 1. 任务总目标

在 **不破坏现有功能、不回退已发布题库、绝不向学生推送乱码** 三条红线内，对系统进行三件事：
**A. 代码质量重构（纯工程）｜B. 内容补全（需数据/VLM）｜C. 能力完善（按价值判断取舍）**。

你应把工作拆成小步、每步可验证、每步小提交。

---

## 2. 工作方向 A —— 代码质量重构（高优先级，纯工程，最安全）

目标：让系统可维护、可测试、可部署，行为**保持等价**。

- **收敛 8014 重复实现**：`src/workbench8014/` 下 `api_app.vision.py` / `api_app.original.py` / `api_app.candidate-ui.py` 三份 + `src/tools/api_app.py` 工作区副本，本质是同一服务的多版本。请以 `api_app.vision.py`（当前生产版，含 SQL 注入修复、PUT /problems/{id}/content、/images 等）为权威基线，合并为**单一可测试模块**；其余版本删除或转为测试 fixture。保留 `/api` 挂载与现有路由。
- **消除硬编码绝对路径**（这是最大技术债）：
  - `run_workbench_8014.py` 里的 `WORKBENCH_DB`、`IMAGE_ROOT`（指向 `D:/My File/...` 与 `D:/workbuddy/...extract_img`）；
  - `app/config.py` 的 `database_path`/`upload_dir` 虽用 `PROJECT_ROOT` 相对，但 `evidence_api_url`/`qwen_grading_url` 是写死的 `127.0.0.1`；
  - `problems.crop_image_path` 指向 `book/...`，真实裁切图位置靠 `IMAGE_ROOT` 拼；
  - `grading_pipeline` 里 poppler `pdftoppm` 路径写死。
  → 全部改为**配置化**（环境变量 / 配置文件 / 启动参数），默认值本地可跑，生产可覆盖。
- **统一测试**：把 `src/tools/` 下散落的 `validate_agent_reports.py` / `regress_student_submit.py` / `regression_where_clause.py` 等收敛为 `pytest` 套件，CI 可一键跑。
- **消除 `questions` INSERT 列序脆弱点**：`app/db.py` 的 `_upsert_local_cache` 用位置参数绑定 9 列，曾因列序错把 `problem_no` 落进 `source_problem_id`（污染 §5.2）。改为**命名参数或 ORM**，从根上杜绝列序错位。
- **前后端解耦（可选）**：`app/teacher_portal.html`、`app/student_submit.html`、`app/answer_import_review.html` 内嵌于 `app/` 与后端耦合，可模板化或前后端分离。
- **保留启动方式**：`run_workbench_8014.py` 的 importlib 加载 + `/api` 挂载必须保留（或提供等价更优方案），不能退化为裸 uvicorn。

---

## 3. 工作方向 B —— 内容补全（中优先级，需数据/VLM，先确认数据可用性）

目标：把题库从「已发布约 362 题」扩充，并修复损坏项。详见 `BACKLOG.md` 与 `src/tools/gap_analysis_8014.py` 的 113 缺口分类：

- **CORRUPT 50**：答案书 OCR 损坏或 8014 已存字段本身乱码 → 回答案书源图走 VLM `/solve-from-image` 重识别，或整章重 OCR；重识别后走 `ai_stem_candidates` 待教师复核批准。
- **NEED_TEXTBOOK 13 / ABSENT 44**：缺题干/答案 → 教材 crop 经 VLM/Pix2Text 识别，走待复核流补全。
- **39 道 FW-blocked（无裁切图）**：这些题 `crop_image_path` 为空、无对应题图，VLM 无法重识别。**严禁用全角→半角规整强行放出**（会把含中文乱码的内容推给学生，违反红线）。必须提供教材/答案 PDF 按题 crop 或 IMA 原页图后才能处理。
- **下册（§9.x 等）尚未导入**：按 `ROUTE2_IMPORT_GUIDE.md` + `src/tools/route2_pdf_extract.py` 管道导入。
- 每补一批，跑 `bulk_sync_8014.py` + `scan_corrupt_8014.py` 校验，确保不引入乱码。

> ⚠️ 方向 B 强依赖外部数据（裁切图 / 教材 PDF / IMA 知识库）。**数据不到位时标注阻塞，不要编造数学内容**（现有 `route2_chapter_importer.py` 已遵守"不伪造"原则，请保持）。

---

## 4. 工作方向 C —— 能力完善（可选，按价值判断取舍）

- **VLM 3B 错位**：按题号从整份作业定位单题偶发张冠李戴，目前全被 `needs_review` 兜底。可增强题号两段式定位器 / 置信门禁，降低 `needs_review` 比例，但**不得把 needs_review 当失败**。
- **教学报表增强**：更多维度、CSV/PDF 导出（现有 4 类：review-quota / weak-points / semester-summary / summary）。
- **知识点打标扩面**：`db.tag_knowledge_points` 目前覆盖有限章节，可扩展到全库。
- **组卷策略**：难度配比（基础/提高/综合 3-2-1）、去重、按薄弱点出题。
- 以上均**不得破坏现有端点契约**。

---

## 5. 硬性约束（红线，违反即不合格）

1. **绝不向学生推送任何乱码/损坏题目**——保留 `garbage_score` 等价损坏门禁；重构后 `published` 中乱码/替换符字符 = 0。
2. **已发布题库不回退**：`questions` 已发布 ≥ 362 题（补全后只增不减）。
3. **保留全部现有端点与业务功能**：`sync-section→publish`、`assignment PDF` 生成、`grade-homework` 批改流水线、4 类报表、`ai_stem` 复核、学生端加固（白名单/30MB/409/绝对路径）。
4. **SQL 安全**：已修复的注入点（参数化 `_where_clause`）不得回退为字符串拼接；新增查询一律参数化。
5. **三服务可启动性**不得破坏；Windows 下杀进程用 `os.kill(SIGTERM)`。

---

## 6. 验收 / 回归基线（必须达标）

| 基线 | 期望 |
|------|------|
| `validate_agent_reports.py` | 全绿（报表聚合正确） |
| `regress_student_submit.py` | 全绿（422/415/409/413 正确） |
| `regression_where_clause.py` | 4/4 PASS（注入不绕过） |
| `bulk_sync_8014.py` 后 `questions` | 补全后 ≥ 原 362 published + 22 blocked，不回退 |
| 端到端 | 提交→批改→复核→写回闭环可用（参考 `docs/闭环端到端验证报告.md`） |
| `published` 乱码/替换符 | = 0 |

---

## 7. 给你的工作流建议

1. 从现状基线绿开始；任何改动前先确认三个回归脚本绿（这是你的"安全网"）。
2. 优先做**行为保持的等价重构（方向 A）**，每改一块跑对应测试，再提交。
3. 方向 B/C 在开始前列出「数据可用性检查表」：裁切图是否到位？教材 PDF 是否提供？IMA 是否授权？不到位就显式标记阻塞。
4. 每完成一个方向/子任务，更新 `HANDOVER.md` / `BACKLOG.md` / `ENVIRONMENT.md` 反映新状态、新地雷、新验收数字。
5. 不要一次性大改写全部代码；小步快跑，便于回滚与审阅。

---

## 8. 交付物要求

- 改动后的代码（可 `git push` 的仓库，或开 PR 到指定远程）。
- 更新后的 `HANDOVER.md` / `BACKLOG.md`（记录新完成项、新踩坑、新验收）。
- 回归测试报告：三个脚本输出 + 端到端证据（如新增题库则给 gap 减少数字）。
- 若改了环境/启动方式，`ENVIRONMENT.md` 同步更新。

---

## 9. 关键文件索引（你该精读哪些）

| 文件 | 作用 |
|------|------|
| `HANDOVER.md` / `ENVIRONMENT.md` / `BACKLOG.md` | 总览 / 环境 / 待办 |
| `src/agent8000/app/main.py` | 8000 全部 API 端点（56k，核心入口） |
| `src/agent8000/app/db.py` | 本地缓存 schema + `_upsert_local_cache`（列序陷阱所在） |
| `src/agent8000/app/knowledge_bridge.py` | Route 1 VLM 识别题干 + 标准答案二次校验 |
| `src/agent8000/app/grading_pipeline.py` | 批改流水线（含大 PDF 分页修复） |
| `src/agent8000/app/ai_stem_review.py` | 待复核候选写回 8014 + 本地 |
| `src/workbench8014/api_app.vision.py` | 8014 权威实现（SQL 注入已修复） |
| `src/vlm18080/server_vlm_service.py` | VLM 服务（/grade-homework 题号定位、partial 重试） |
| `src/tools/bulk_sync_8014.py` | 8014→Agent 同步 + 损坏门禁 |
| `src/tools/gap_analysis_8014.py` | 113 缺口分类 |
| `src/tools/recover_corrupt_269.py` / `release_safe_fw.py` | CORRUPT 恢复 / 安全放出（红线示范） |
| `src/tools/validate_agent_reports.py` / `regress_student_submit.py` / `regression_where_clause.py` | 回归基线 |
| `db_schema/` | 两库结构与行数（无需 24MB 二进制即可看懂） |
| `docs/` | 设计/诊断/验证文档 |

---

> **给用户的提示（你不发给 Codex，自己看）**：把本文件与 `CODEX_HANDOVER.zip` 一起交给 Codex。若你希望 Codex 优先做某一方向（例如"只做 A 代码重构，不动题库"或"重点补全 §5 之后章节"），在第 1–4 节前加一句你的优先级说明即可。Codex 若需要教材 PDF / 裁切图 / VLM SSH 凭据 / IMA 授权，会向你索要——这些不在压缩包内。
