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
   - 前端 `teacher_portal.html` 新增常驻入口「✎ 题目直接编辑」与 `#editq` 分区（加载 / 保存 / 乱码队列导航 / 校验提示 / review_status 可改 blocked）；乱码队列使用多行文本控件，默认按当前 `content/answer/rubric` 做只读实时质量扫描，展示高/中风险候选而不自动改写或改变发布状态。

2. **LaTeX 实时预览**：教师端页面引入 **MathJax 3**（tex-svg），编辑面板题干/答案/评分参考三个文本框**边输入边渲染**（仅渲染带 `$...$` 定界符的公式，与打印页语义一致）。

3. **PDF 作业单 LaTeX 渲染**：无 TeX 引擎环境下改用 **matplotlib mathtext 混合引擎**（`assignment_pdf.py`）；不支持 `\begin{...}` 环境，自动降级为可读多行文本；HTML 端走 MathJax。仓库版 `assignment_pdf.py` 已适配仓库相对路径（`src/tools/build_worksheet.py`）。

4. **题库全角→半角归一**：`questions` 全表 394 题 `content/answer/rubric` 全角 ASCII 归一（`＝→=`、`－→-`、`（）→()`、`Ａ→A`、`０→0`），**341 题被修正**（修掉会让 PDF mathtext 解析失败的全角运算符）。备份 `homework_before_fullwidth_20260824_192809.db`（在生产目录 `data/`，不入库）。

5. **乱码复核清单**：`docs/garble_audit.csv` —— **102 道「汉字代字母」深层 OCR 乱码题**（85 published），按被作业引用数排序（q250/18、q251/17 优先）。8014 端 477 题中 364 题题干+答案齐全；113 题缺口分类：CORRUPT 50 / NEED_TEXTBOOK 13 / ABSENT 44（另有 6 道有父题的子题，设计刻意跳过）。

6. **重 OCR 脚本**（`vlm_reocr_questions.py`，在用户工作区、未入库）：对 **14 道有原图**的乱码题调 8014 `/review` 重识别并回填 `answer/rubric`；dry-run 安全，`--apply` 在 VLM 不可达时优雅跳过、0 写入。当前 VLM 仅经 **SSH 隧道**可达（`222.211.217.7:10022` 返回 SSH banner，非直连 HTTP）。

7. **安全修复**：`route2_chapter_importer.py` 的**硬编码 SSH 密码改为环境变量读取**（`VLM_SSH_HOST/VLM_SSH_PORT/VLM_SSH_USER/VLM_SSH_PASSWORD`）。
   > ⚠️ **注意**：旧 commit 历史中仍含该密码明文。**请尽快轮换该 SSH 密码**（若仓库非私有，强烈建议重写历史）。

8. **批量 PDF 取证闭环（2026-08-25）**：新增 `build_live_pdf_evidence_plan.py`（只读生成当前 8000 疑似乱码题的来源清单）和 `render_pdf_evidence_pages.py`（显式 `--render` 才输出登记 PDF 页 PNG，绝不改题库）。只有已有单题裁切图的 `ready_for_teacher_review` 条目可交给已有 `stage_image_review_candidates.py` 暂存 VLM 候选；教师在 8000 候选复核中确认后才写回。当前实际库的 120 条候选均有 8014 绑定，但 `textbooks.pdf_path`、`problems.source_page` 和本地 `extract_img/` 均未提供可用来源，故全部停在 `needs_source_evidence`，未生成任何候选或题库写入。

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
6. **PDF 取证元数据尚未补齐**：当前 8014 的教材记录未登记 `pdf_path`，题目未登记 `source_page`，且仓库没有 `extract_img/`。本地虽有 PDF 文件，但不能可靠反推“某题对应哪一页/哪一块”。请提供教材/答案裁切图，或授权可追溯的 IMA 原页资料；VLM SSH 隧道配置完成后，才可对已取证的单题图批量生成 LaTeX 候选。

---

## 6. 建议 Codex 优先事项

1. **先跑通**：按 §3 起三服务、跑 §4 回归基线，确认接手时是绿的。
2. **方向 A（代码质量，最安全）**：消除硬编码路径、收敛 8014 多版本、`questions` 列序改命名参数、pytest 统一 —— 详见 CODEX_BRIEF §2。
3. **方向 B（内容补全，需数据）**：按 `docs/garble_audit.csv` 与 `src/tools/gap_analysis_8014.py` 分类推进；数据不到位标注阻塞，**不编造数学内容**。
4. **方向 C（能力完善）**：编辑面板可加「LaTeX 自动包 `$`」工具、「8014 原图对照」；组卷策略等 —— 详见 CODEX_BRIEF §4。
5. **红线**（CODEX_BRIEF §5）：不乱码推题、题库不回退、端点契约不破坏、SQL 参数化、三服务可启动。

## 17. 多教师上线基础（2026-08-26）

- 8000 已加入可选认证层：`AUTH_REQUIRED=false` 保持原本机单教师体验；正式部署必须设为 `true`，并通过 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` 首次创建管理员。
- 角色为 admin / teacher / student。教师数据以 `classes.teacher_user_id` 隔离；学生需邀请码且必须匹配已导入名单的学号、姓名才可激活并提交。题库中官方已核验题可读，教师私有题不可相互读取或修改。
- 新增 `users`、`user_sessions`、`class_invites`、`audit_logs` 表与迁移，且有 `tests/test_auth_tenancy.py` 覆盖教师隔离和学生邀请码激活。
- 公网部署仅暴露 8000；8014 与 18080 必须留在内网/loopback。生产部署、HTTPS、备份 timer 与数据保留默认规则见 `src/agent8000/deploy/PRODUCTION_MULTI_TENANT.md`。
- **外部阻塞**：尚无域名、应用服务器、DNS 及学校最终隐私/保留制度，故不能宣称公网 HTTPS 已上线；这些到位后按部署清单实施并做恢复演练。

## 18. 隔离演示包与真实 VLM 验收（2026-08-27）

- 教师端新增“🧪 一键演示包”。它创建 `DEMO` 班级、演示学生、非发布演示题和 10 分作业，并提供可下载的手写 PNG；演示作业带 `assignments.is_demo=1`，不会计入正式报表。
- 实测本机 SSH 隧道后的 A100 VLM：`/health` 返回 8×A100 且模型已加载；`/grade-homework` 对样本定位到第 1 题，识别 `1+0=1`，返回 10/10、confidence 1.00。工作台提交→初评→教师确认→报表隔离同样通过。
- 结果仍进入教师复核，原因是安全门禁优先于模型置信度；这是预期行为，绝不因演示样本自动发布成绩。

---

## 7. 本次交付物确认

本次 commit 包含：最新业务代码（编辑面板 / MathJax 预览 / PDF 引擎 / 8014 回写）、安全修复（SSH 凭据环境变量化）、`docs/garble_audit.csv` 乱码复核清单、`question_bank_review.py`、`tests/`、`src/grading_engine.py`、`src/tools/mineru_knowledge_pipeline.py`、本交付说明。

---

## 8. 2026-08-25 Milestone 1A：教材来源证据基础

- 已新增可复跑的 PDF 盘点、SHA-256 验证、相对根路径解析和 8014 来源登记。原始 PDF 不入库；题干、答案、`published`/`blocked` 均未改动。
- 8014 新增 `textbook_documents`（来源文件）和 `problem_source_anchors`（未来页/框锚点）两张表；本次仅登记 7 个文件映射，尚未写入任何题目锚点。
- Route2 §5.1–§5.6 已按用户确认映射到 `李继成高数-答案-下册-OCR.pdf`：303 页、295 页有原生文本、文本层比例 97.36%。上册教材为 329 页纯扫描 PDF。
- 证据清单与操作说明见 `docs/textbook_document_registration.json`、`docs/SOURCE_EVIDENCE_FOUNDATION.md`；风险快照为 120 个 `RISK_CANDIDATE`（44 高、76 中），不等同于已确认错误。
- 下一步 1B 仅对已有“页码/框选/来源图”的单题建立 `problem_source_anchors`，再输出教师确认的 LaTeX 候选；无证据时继续标注阻塞，禁止猜测数学内容。

## 9. 2026-08-25 Milestone 1B：Route2 页锚点候选

- 新增 `src/tools/propose_route2_answer_anchors.py`：只读取 PDF 原生文本块，通过“习题章节标题 + 题号 + 题干前缀相似度”建立来源坐标；不调用 OCR/VLM、不修改 `problems`，也不改任何复核或发布状态。
- 通过三重门禁的 14 条记录已写入 `problem_source_anchors`，状态均为 `candidate`；34 条仍为 `needs_teacher`，17 条因没有可靠编号证据而阻塞。完整审计见 `docs/route2_anchor_reviews/2026-08-25-applied.json`。
- PDF 的 §5.6 在“总习题五”前仅有前两题，代码显式截断该边界，避免把后续总习题的同号内容误锚定为 §5.6。

## 10. 2026-08-26 OCR Repair Agent：候选闭环（不写回题库）

- 无 bbox 定位器已对 Route2 §5.1–§5.6 的原生 PDF 文本按“章节标题 → 题号 → 同页下一题（末题到页脚）”生成锚点；低相似度或无题号证据的记录仍停在 `needs_teacher`／`blocked`，不会猜测题目内容。14 条通过门禁的记录均为 `candidate`，裁切图仅存于 `answer_source_previews/candidate_anchor_crops/`，未写入 `problems.crop_image_path`。
- A100 已实装并实测 MinerU pipeline、PP-FormulaNet_plus-L 与现有 18080 VLM。三路结果只写入 `ocr_repair_candidates`；该表和 `ocr_repair_decisions` 是独立的教师复核证据表。
- 数学安全决策已实现 `AUTO_ACCEPT` / `AUTO_REPAIR` / `NEEDS_TEACHER_REVIEW`，对关键比较符号、上下标、分式、积分界等冲突保守拦截。当前 14 条真实候选均为 `NEEDS_TEACHER_REVIEW`：MinerU / FormulaNet 尚无可校准的整题置信度，故未自动采纳。
- 教师端入口为 `http://127.0.0.1:8014/ocr-repair`：显示候选原图、当前题库文本、三路 LaTeX 候选、风险及确认/拒绝。确认只更新 `ocr_repair_decisions.teacher_status`，接口明确返回 `question_bank_written:false`，绝不修改 `problems`。
- 审计：`docs/route2_anchor_reviews/2026-08-26-three-provider-audit.json` 记录 MinerU 14/14、PP-FormulaNet 14/14、VLM 对这 14 个锚点的候选（含历史重跑共 28 条）；所有 14 条为 `NEEDS_TEACHER_REVIEW`。
- 验证：隔离 DB 下的 HTTP 端到端检查已验证候选列表、候选裁图 200、确认操作，以及 `content_text/std_answer/full_solution` 均保持不变；目标相关 pytest 14/14 通过。保留的全量 pytest 中历史 `test_regression_scripts.py` 超出常规执行时长而被单独隔离，未作为本模块通过凭据。
- 教师端现已升级为可编辑的 `content_text` / `std_answer` / `full_solution` 分栏。仅当教师填写题干与标准答案、二次确认并点击“确认并写回题库”时，才写入 `problems` 并设为 `verified`；同一事务会保存 `ocr_repair_writebacks` 的前后快照和教师备注。空标准答案、未显式 `confirm=true` 或未通过既有答案质量门禁均拒绝写回。
- 2026-08-26 教师已确认的 Route2 14 条候选已全部经显式写回接口入库：`ocr_repair_decisions.committed=14`、`ocr_repair_writebacks=14`。批量写入只复用了教师确认后的现有字段，未生成或猜测数学内容。
- 后续风险题取证已启动并生成 `docs/route2_anchor_reviews/2026-08-26-garble-evidence-plan.json`：当前实时队列为 120 条（旧 `garble_audit.csv` 是 102 条静态快照），44 条高风险；120 条均缺可验证 PDF 页或本地裁图，因而本批 `ready_for_teacher_review=0`。其中 89 条缺 8014 来源绑定，31 条有登记裁图路径但文件缺失。它们必须待补教材／答案页或 IMA 证据后才能生成候选。

## 11. 2026-08-26 OCR Repair 教师编辑体验

- OCR Repair 写回接口允许教师保存多行 `std_answer`（例如多组傅里叶系数或方程组），不再把换行本身误判为 OCR 串题；仍保留空值、乱码、页眉和超过 3000 字符的跨题内容门禁。
- 多行标准答案不会进入自动评分：`grading_ready` 仍会将其留在人工复核路径，避免改变学生端自动判分语义。
- `http://127.0.0.1:8014/ocr-repair` 的题干、标准答案、完整解答编辑框均提供输入即刷新的 MathJax LaTeX 预览；写回前仍须教师对照原图并显式确认。

## 12. 2026-08-26 Windows 单机试点落地

- `src/agent8000/deploy/start_windows.bat` 已改为从仓库位置推导 8000/8014 的代码、数据库与候选图路径，移除过期的 `D:/workbuddy` 启动器依赖；8014 仍经正式启动器加载。
- 新增 `healthcheck_windows.ps1` 和 `backup_databases.ps1`，分别用于启动后健康检查与题库操作前的双 SQLite 备份；单机操作说明见 `src/agent8000/deploy/LOCAL_PILOT.md`。
- 已实测本机 8000 capabilities、8014 health 和 OCR Repair 页面均返回 HTTP 200。正式对学生开放仍需要受控服务器/HTTPS、认证和管理台访问限制；不能将 8000/8014 端口直接公网暴露。

## 13. 2026-08-26 章节标题清理

- 已在备份后将 28 个历史章节标题中的“（答案书补录）”移除，统一显示为“习题 X.X”；该标签只是过去的来源记录，不代表待补录任务。本次不改题目、答案或审核状态。

## 14. 2026-08-26 作业打印 LaTeX 修复

- 修复 8000 可打印作业页对既有 `\\(...\\)` / `\\[...\\]` 定界符的二次包装问题；此前会生成非法的 `\\( $...$ \\)` 嵌套，导致公式红色原样显示。
- 打印显示层会移除答案书 OCR 遗留的“页码 + 章节名”行，不改数据库中的题干、答案或来源证据。单元测试覆盖已有行内定界符和页眉过滤。

## 15. 2026-08-26 统一组卷编号与分值

- 新建作业统一按作业内排序显示 `1、2、3…`，不再显示教材原始题号；显示与 PDF/LaTeX 导出均会移除题干开头的教材题号，避免出现“双题号”。
- 每题固定 10 分，作业总分按题数自动计算。已有学生提交的历史作业保留原分值；本次仅将无学生提交的现有作业迁移为新规则，并在迁移前备份两份数据库。

## 16. 2026-08-26 班级名单与真实教学报表

- 8000 教师端新增“班级与名单”：可创建班级与学期，并导入首行含“学号、姓名”的 `.xlsx` 或 UTF-8 CSV 名单；重复学号只更新姓名，不会重复建档。名单文件其他列可保留，但系统不会猜测身份列。
- 新发布作业必须选择已建班级且该班已有名单；作业记录绑定 `class_id`。学生提交时会核对学号是否在该班名单中，未在册学号不会被接收。
- 教学报表、仪表盘中的作业/提交数、复核配额和学期汇总只统计绑定 `class_id` 的真实班级作业。旧版无 `class_id` 的样例记录保留但不再展示在真实教学报表，避免“王小明”等演示数据污染开学后的统计。
- 验证：隔离 SQLite 下完成建班、CSV/XLSX 名单导入、名单外提交拒绝、名单内提交接受、真实班级成绩汇总和旧演示记录排除；相关 pytest 6/6 通过。

## 17. 2026-08-26 全链路沙箱演练

- 已在临时 SQLite、临时上传目录中实际演练“建班与名单 → 从运行中 8014 同步 §5.1 → 自动组卷 → 可打印作业（每题 10 分）→ 名单内学生提交 → 自动批改 → 教师确认 → 学期报表/复核配额”。测试库和文件已在结束后删除，未写入正式题库或教学数据。
- 初次演练时 VLM 评分端点曾返回 **502 Bad Gateway**，系统正确把该提交标记为 `review_required`，没有将 0 分自动发布；该故障降级链路已验证。
- 18080 恢复后，已再次跑真实 VLM 集成验收：班级与名单、自动组卷、打印、提交、A100 图像评分、数学安全复核门、教师确认和报表均通过。测试样张的模型初评为 10/10、置信度 1.0；符号判等器对该简化表达式保守要求复核，教师确认后才进入报表。临时 SQLite 与文件均已删除，未污染正式教学数据。
