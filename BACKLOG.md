# 待办与重构范围（Codex 交接）

## A. 已完成（已验证，不得回退）

### 架构 / 部署
- [x] 三层架构（8014 证据库 / 8000 智能体 / 18080 VLM）全部跑通
- [x] 生产部署交付物：`deploy/`（Docker / supervisor / nginx / start 脚本 / requirements 冻结）
- [x] `TESTING.md` 自测指南

### 题库恢复
- [x] IMA 答案书 OCR 回填（`recover_from_answer_ocr.py --fix`），8014 题干+答案齐全 364 题
- [x] 8014→Agent 同步（`bulk_sync_8014.py`，`garbage_score` 损坏门禁），Agent ≈ 362 published + 22 blocked
- [x] 缺口分类（`gap_analysis_8014.py`）：FREEWIN 0 / CORRUPT 50 / NEED_TEXTBOOK 13 / ABSENT 44
- [x] §2.6/#2.7/#2.9 CORRUPT 章全角→半角规整回填（`recover_corrupt_269.py`）
- [x] 安全放量（`release_safe_fw.py`）：仅放出人工核验干净的 2 道，拒绝推乱码

### Agent 功能
- [x] Route 1：VLM 识别题干 → pending_review → 教师审批 → 写回 8014（`knowledge_bridge` + `ai_stem_review`）
- [x] Route 2：章节导入器（`route2_chapter_importer.py` + `route2_pdf_extract.py`），§5.1–5.6 已导入 verified
- [x] 组卷 + 出 PDF（`assignment_pdf.py`）实测 15MB 合法 PDF
- [x] 大 PDF 分页批改修复（49 页张三复批 73/100）
- [x] 4 类教学报表（review-quota / weak-points 概念级 / semester-summary / summary）
- [x] 知识点打标（`db.tag_knowledge_points`，零 VLM 依赖）

### 学生端
- [x] 提交加固：学号白名单 / 扩展名白名单 / 30MB / 409 / 绝对路径 / grading_job_id
- [x] `regress_student_submit.py` 全绿

### 安全 / 验证
- [x] SQL 注入修复 + 全量审计（4 份 api_app 实现），`regression_where_clause.py` 4/4
- [x] `validate_agent_reports.py` 全绿；`闭环端到端验证报告.md`（王小明 84/96）

## B. 待办（内容补全，需人工/VLM，非代码 blocking）

### 题库缺口 113 题（按根因）
- [x] 已生成只读复核就绪清单（2026-08-16）：17 条可凭现有裁图进入候选复核，92 条缺源图阻断；未自动写库或发布
- [ ] 17 条裁图候选：待明确授权将教材裁图发送至远端 VLM 后，仅暂存为 `pending`，再由教师逐条批准/拒绝
- [x] 17 条裁图候选已获授权并执行暂存（2026-08-16）：9 条 pending，8 条 VLM 未生成可审题干，保持阻断
- [ ] **CORRUPT 50**：答案书 OCR 损坏 / 8014 已存字段乱码 → 回答案书源图走 VLM `/solve-from-image` 或整章重 OCR
- [ ] **NEED_TEXTBOOK 13**：答案书该栏空 / 父题干仅题号 → 教材 crop 经 VLM/Pix2Text，走 `ai_stem_candidates` 待复核（§7.6×5、§3.4×2、§8.6×2、§3.9/§6.8/§7.5/§9.9 各1）
- [ ] **ABSENT 44**：答案书无此题号 → 分散各章，需补源

### 39 道 FW-blocked（特殊难例）
- [ ] 全角规整会推出含中文乱码的题目 → 已被 `release_safe_fw.py` 安全拦截
- [ ] 全部 `crop_image_path` 为空且按命名约定链接 0 道存在对应题图 → **VLM 重识别不可行**
- [ ] 修复路径：提供教材/答案 PDF 按题 crop → `vlm_reoc_8014.py`（已就绪，待图）；或从 IMA 取原页图走 `/review`

### 残留符号级瑕疵（非阻塞，建议 VLM 精修）
- [ ] §2.6#5 stem `使得 f'(~) =一一τ-`、§2.9#11 answer 短含 `~`、§2.6#6 answer 零星 mojibake（长解答混杂字，判定可用）

### 未导入章节
- [ ] 下册 §9.x 等尚未注册/导入；两本教材 PDF 在磁盘但未全量录入

## C. Codex 重构重点

### 必保功能（验收基线）
- sync-section → publish、assignment PDF、grade-homework 流水线、4 类报表、ai_stem 复核、学生端加固
- 损坏门禁（不向学生推乱码）；已发布 ≥ 362 题不回退

### 代码质量目标
- [x] 收敛 8014 多份 `api_app`：`api_app.py` 为唯一生产模块，旧副本已归档到 `legacy/`（2026-08-16）
- [x] 旧 8014 副本已补充归档说明；运行入口、部署脚本和回归均只使用权威模块（2026-08-16）
- [x] 8014 启动路径、回归脚本临时路径与 8000 外部服务地址的首批配置化（2026-08-16）
- [x] AI 候选写入 `questions` 改为命名参数，消除该路径的列序绑定风险（2026-08-16）
- [ ] 收敛 8014 三份重复 `api_app`（vision/original/candidate-ui）+ 工作区副本 `api_app.py` → 单一可测试模块
- [ ] 消除硬编码绝对路径（`run_workbench_8014.py` / `config.py` / crop 路径 / poppler 路径）→ 配置化
- [ ] `questions` INSERT 列序脆弱点 → 命名参数 / ORM（见 HANDOVER.md §4.1）
- [x] 散落回归脚本 → 统一 `pytest` 套件（2026-08-16；临时 SQLite、无二进制测试库依赖）
- [ ] 学生端/教师端 HTML 内嵌 `app/` 与后端耦合 → 前后端分离或模板化
- [ ] 引入正式 git 工作流（本仓库已初始化）

### 外部依赖交接（见 HANDOVER.md §6 步骤 5）
- 远端 VLM SSH 凭据（222.211.217.7）、IMA 知识库授权（ID 7491467723418571）、可选 DIFY key

## D. 验收基线（重构后对照）
- `validate_agent_reports.py` ALL VALIDATIONS PASSED
- `regress_student_submit.py` 全绿
- `regression_where_clause.py` 4/4
- `bulk_sync_8014.py` 后 Agent `questions` ≈ 362 published + 22 blocked
- 端到端：提交→批改→复核→写回闭环可用
- published 中乱码/替换符 = 0
