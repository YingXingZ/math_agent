# Codex 交付说明（2026-08-25）

> 本文是给 **Codex（执行方）** 的交接说明。本仓库已更新为**当前最新可运行版本**（以生产运行目录 `高数作业助手/` 为基准合并而来）。
> 请先读本文，再按需精读：`CODEX_BRIEF.md`（任务指令/红线/验收基线）、`HANDOVER.md`（进程状态/已知地雷）、`ENVIRONMENT.md`（环境/端口/依赖）、`BACKLOG.md`（待办清单）。

---

## 0. 三句话说明

- 系统 = **高数教材答案 Agent 工作台**：`8000` 教师端智能体 + `8014` 证据库 + `18080` 远端 VLM（`222.211.217.7`）三服务。
- 本仓库 = **当前最新代码**（含 2026-08 新增的「题目直接编辑面板 / LaTeX 实时预览 / 保存回写 8014 / PDF 渲染修复」）。
- 运行方法见 §3，验证基线见 §4，已知问题与技术债见 §5。

---

## 1. 本次更新内容（相对上次 handover 的新增）

1. **题目内容直接编辑面板（教师端）**
   - 后端新增：`GET /api/questions/{question_id}`（读单题，不限 review_status）、`PUT /api/questions/{question_id}`（PATCH 式可选字段，**不阻塞**数学校验、仅返回 `validation_warning`）、`GET /api/garble-queue`（读乱码待修队列，按被作业引用数排序）。
   - `PUT` 带 `sync_8014=true`（默认）时，按 `source_problem_id` 把 `content→content_text`、`answer→std_answer`、`rubric→full_solution` **写回 8014 证据库**并清空 `answer_invalid_reason`；8014 写失败**不影响**本地保存；无对应记录时返回 skipped 不报错。
   - 前端 `teacher_portal.html` 新增常驻入口「✎ 题目直接编辑」与 `#editq` 分区（加载 / 保存 / 乱码队列导航 / 校验提示 / review_status 可改 blocked）。

2. **LaTeX 实时预览**：教师端页面引入 **MathJax 3**（tex-svg），编辑面板题干/答案/评分参考三个文本框**边输入边渲染**（仅渲染带 `$...$` 定界符的公式，与打印页语义一致）。

3. **PDF 作业单 LaTeX 渲染**：无 TeX 引擎环境下改用 **matplotlib mathtext 混合引擎**（`assignment_pdf.py`）；不支持 `\begin{...}` 环境，自动降级为可读多行文本；HTML 端走 MathJax。仓库版 `assignment_pdf.py` 已适配仓库相对路径（`src/tools/build_worksheet.py`）。

4. **题库全角→半角归一**：`questions` 全表 394 题 `content/answer/rubric` 全角 ASCII 归一（`＝→=`、`－→-`、`（）→()`、`Ａ→A`、`０→0`），**341 题被修正**（修掉会让 PDF mathtext 解析失败的全角运算符）。备份 `homework_before_fullwidth_20260824_192809.db`（在生产目录 `data/`，不入库）。

5. **乱码复核清单**：`docs/garble_audit.csv` —— **102 道「汉字代字母」深层 OCR 乱码题**（85 published），按被作业引用数排序（q250/18、q251/17 优先）。8014 端 477 题中 364 题题干+答案齐全；113 题缺口分类：CORRUPT 50 / NEED_TEXTBOOK 13 / ABSENT 44（另有 6 道有父题的子题，设计刻意跳过）。

6. **重 OCR 脚本**（`vlm_reocr_questions.py`，在用户工作区、未入库）：对 **14 道有原图**的乱码题调 8014 `/review` 重识别并回填 `answer/rubric`；dry-run 安全，`--apply` 在 VLM 不可达时优雅跳过、0 写入。当前 VLM 仅经 **SSH 隧道**可达（`222.211.217.7:10022` 返回 SSH banner，非直连 HTTP）。

7. **安全修复**：`route2_chapter_importer.py` 的**硬编码 SSH 密码改为环境变量读取**（`VLM_SSH_HOST/VLM_SSH_PORT/VLM_SSH_USER/VLM_SSH_PASSWORD`）。
   > ⚠️ **注意**：旧 commit 历史中仍含该密码明文。**请尽快轮换该 SSH 密码**（若仓库非私有，强烈建议重写历史）。

---

## 2. 仓库结构

```
src/agent8000/           8000 智能体（app/ 全部业务代码）
  app/main.py            全部 API 端点（含编辑面板/garble-queue/sync_8014）
  app/teacher_portal.html 教师端（含「题目直接编辑」+ MathJax 预览）
  app/question_bank_review.py  题库复核队列模块（独立可用，尚未被 main.py 接线）
  app/db.py / config.py / grading_pipeline.py / knowledge_bridge.py / ...
src/workbench8014/       8014 证据库（api_app.py 生产版 + legacy/ 旧版归档）
src/tools/               同步/巡检/回归脚本 + build_worksheet.py + mineru_knowledge_pipeline.py
src/grading_engine.py    符号判等引擎（grading_pipeline / knowledge_bridge 依赖）
src/vlm18080/            VLM 服务端
tests/                   pytest（test_regression_scripts.py / test_question_bank_review.py）
docs/                    设计/诊断/复核清单（garble_audit.csv、QUESTION_BANK_REVIEW.md 等）
根目录                    CODEX_BRIEF.md / HANDOVER.md / ENVIRONMENT.md / BACKLOG.md / README.md
```

---

## 3. 运行方法（要点复述，细节见 ENVIRONMENT.md）

- **解释器**：托管 venv `C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（3.11 依赖集，含 fastapi/uvicorn/matplotlib/fitz/PIL）。
- **8014**：`python src/workbench8014/run_workbench_8014.py`（importlib 加载、挂 `/api`；`WORKBENCH_DB` / `IMAGE_ROOT` 需固化，否则 `/images/` 404）。
- **8000**：`cd src/agent8000 && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。
- **18080 VLM**：远程 `222.211.217.7:10022`（SSH 隧道端点）；本机/纯 HTTP 环境不可直连，探测超时属正常。
- **Windows 杀进程**：`os.kill(pid, signal.SIGTERM)`；`Stop-Process`/`wmic` 在沙箱被拦截。

---

## 4. 验证基线（本次已实测）

| 检查 | 结果 |
|------|------|
| repo 代码独立冒烟（8001 端口，空环境启动） | `/`、`/api/questions`、`/api/questions/250`、`/api/garble-queue`、`/api/agent/capabilities` **全部 200**；根页面含「题目直接编辑」+ MathJax ✓ |
| 生产库端到端 | GET/PUT 改→持久化→还原完全一致；缺失题号 GET/PUT 均 404；q246 过校验；8014 回写 `std_answer`/`full_solution` 生效、`answer_invalid_reason` 清空 ✓ |
| 回归脚本 | `validate_agent_reports.py` / `regress_student_submit.py` / `regression_where_clause.py`（历史全绿基线，改动后需重跑确认） |
| pytest | `tests/` 下用例（`test_question_bank_review.py` 依赖 8014 库，空库环境下可独立跑） |

---

## 5. 已知问题 / 技术债（Codex 接手须知）

1. **硬编码绝对路径仍是最大技术债**：8000 教师编辑的 `GARBLE_AUDIT_CSV` 与 `WORKBENCH_DB_PATH` 已改为环境变量，默认分别指向仓库 `docs/garble_audit.csv` 与 `api.workbench.db`；`run_workbench_8014.py` 的 `WORKBENCH_DB`/`IMAGE_ROOT` 与 `grading_pipeline` 的 poppler 路径仍需继续配置化。→ 按 CODEX_BRIEF §2 逐项推进。
2. **88 道乱码题无原图、解答在所有源均乱码**：VLM 也无法恢复 → 只能经「题目直接编辑」面板人工修订，或补教材源图后重 OCR。红线：**绝不向学生推送乱码**。
3. **教师端「待修复题目」复核面板**：src 侧的 UI 改版未合入本次 portal（与 live 后端不一致），本次保留 live 版编辑面板；`question_bank_review.py` 模块已入库待接线。
4. **未合入的备选改进**：src 侧未提交的同步两阶段并发（`RECOG_CONCURRENCY`）、`_vision_call` 重构等未合入（以本地补丁形式留存，含敏感信息，**勿直接入库**，需要时向作者索取）。
5. **`.db` / PDF / `extract_img/` 均不入库**（见 `.gitignore`）；`src/agent8000/data/homework.db` 为运行态本地缓存。

---

## 6. 建议 Codex 优先事项

1. **先跑通**：按 §3 起三服务、跑 §4 回归基线，确认接手时是绿的。
2. **方向 A（代码质量，最安全）**：消除硬编码路径、收敛 8014 多版本、`questions` 列序改命名参数、pytest 统一 —— 详见 CODEX_BRIEF §2。
3. **方向 B（内容补全，需数据）**：按 `docs/garble_audit.csv` 与 `src/tools/gap_analysis_8014.py` 分类推进；数据不到位标注阻塞，**不编造数学内容**。
4. **方向 C（能力完善）**：编辑面板可加「LaTeX 自动包 `$`」工具、「8014 原图对照」；组卷策略等 —— 详见 CODEX_BRIEF §4。
5. **红线**（CODEX_BRIEF §5）：不乱码推题、题库不回退、端点契约不破坏、SQL 参数化、三服务可启动。

---

## 7. 本次交付物确认

本次 commit 包含：最新业务代码（编辑面板 / MathJax 预览 / PDF 引擎 / 8014 回写）、安全修复（SSH 凭据环境变量化）、`docs/garble_audit.csv` 乱码复核清单、`question_bank_review.py`、`tests/`、`src/grading_engine.py`、`src/tools/mineru_knowledge_pipeline.py`、本交付说明。
