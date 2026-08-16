---
name: textbook-ingestion
description: >
  高等数学教材章节的"抽取题目 → 难度分层 → 组卷出 PDF"流水线。当用户要基于
  8014 证据工作台中的某一节（如 1.1）自动生成可打印作业（保留原题号、留答题
  空白、基础/提高/综合 3-2-1 分布）时使用。覆盖三个工具
  (sync_section / stratify_section_difficulty / assemble)、Agent Orchestrator
  (publish_homework) 与对外 HTTP 端点。
---

# 教材章节 ingestion 流水线（textbook-ingestion）

把 8014 权威证据库里的高数教材某一节，变成一份**学生可直接打印、拍照上传**的
作业 PDF。流程严格对应设计文档《作业发布与批改-智能体构想》的 Phase ⑤/⑥：

```
extract (sync_section)  →  stratify (stratify_section_difficulty)  →  assemble (assemble)
   8014 → 本地缓存        基础 / 提高 / 综合 三层标定               选 3-2-1、保留原号、渲染 PDF
```

---

## 1. 架构与职责边界

| 层 | 模块 | 职责 |
|----|------|------|
| 工具 1 | `app/education_document_tools/extract_section.py` | 调 `main._sync_section_into_local_cache`，把 8014 一节的可读题目（经质量门禁、Pix2Text 救援、Qwen 兜底）拉入本地 `questions` 缓存（`review_status='published'`）。不可读/乱码题置 `blocked`。 |
| 工具 2 | `app/education_document_tools/stratify_difficulty.py` | 对 published 缓存行用内容/题型启发式重标 `基础/提高/综合`，使组卷能满足 3-2-1。 |
| 工具 3 | `app/education_document_tools/assemble_assignment.py` | 按 3-2-1 选 `question_count` 题、保留原题号（写 `original_no`）、插入 `assignments`/`assignment_questions`、渲染 A4 PDF。 |
| 编排 | `app/orchestrator.py` | `publish_homework()` 顺序串起工具 1→2→3，并回查每题的 `source_problem_id` 供下游 AI 复核；`grade_due_submissions()` 触发到期提交批改。 |
| 对外 | `app/main.py` | `/api/agent/pipeline/publish`（纯流水线，不依赖 Dify）与 `/api/agent/assignments`（流水线 + Dify AI 复核）。 |

> 质量门禁、校验、批改逻辑**不在本流水线内**——它们分别由 `question_validation.py`、
> `mineru_review.py`、`grading_pipeline.py` 负责；编排器只负责"排序"与"在模块间
> 规整数据"。

---

## 2. 三个工具的函数签名

```python
# 工具 1：抽取一节（经门禁）
async def sync_section(section_no: str, limit: int = 80) -> dict
#   返回 synced_count / skipped_unreadable / ai_candidate_count /
#        unresolved_items / available_by_level / question_ids

# 工具 2：难度分层
async def stratify_section_difficulty(section_no: str) -> dict
#   返回 total_published / distribution / changes

# 工具 3：组卷 + 渲染
def assemble(sections: list[str], *, title: str, class_name: str, due_at: datetime,
             question_count: int = 6, basic_ratio: float = 0.5,
             advanced_ratio: float = 0.35, build_pdf: bool = True,
             out_dir: str | None = None) -> dict
#   返回 assignment_id / selected_ids / composition / pdf_path / page_count / problems
```

`assemble` 的 3-2-1 计算：`基础 = round(n·basic_ratio)`，`提高 = round(n·advanced_ratio)`，
其余为 `综合`（默认 n=6 → 基础3 / 提高2 / 综合1）。

---

## 3. Orchestrator

```python
async def publish_homework(sections: list[str], *, title, class_name, due_at,
                           question_count=6, basic_ratio=0.5, advanced_ratio=0.35,
                           build_pdf=True, out_dir=None) -> dict
```

返回 `assemble` 的结果，并附带：
- `source_problem_ids`：每道已选题对应的 8014 证据 id（供 Dify 复核 / 教师审计引用）；
- `sync` / `stratify`：每节的抽取与分层诊断。

---

## 4. HTTP 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/pipeline/publish` | **纯流水线**（推荐先验证）。Body 同 `AssignmentIn`：`title, chapter, class_name, due_at, question_count, basic_ratio, advanced_ratio`。不调用 Dify，适合联调。 |
| POST | `/api/agent/assignments` | 流水线 + Dify `assignment_review` AI 复核。教师一键发布正式作业。 |
| GET  | `/api/assignments/{id}/pdf` | 下载已生成作业 PDF。 |
| GET  | `/api/assignments/{id}/latex` | 题目 LaTeX 源码（JSON）。 |
| GET  | `/api/assignments/{id}/latex.tex` | 可编译 `.tex`。 |

`AssignmentIn` 默认：`question_count=6, basic_ratio=0.5, advanced_ratio=0.35`
（即 6 题：基础3 / 提高2 / 综合1）。`basic_ratio + advanced_ratio ≤ 1`。

**联调示例：**

```bash
curl -X POST http://127.0.0.1:8000/api/agent/pipeline/publish \
  -H "Content-Type: application/json" \
  -d '{"title":"第七章作业","chapter":"1.1","class_name":"数学A班",
       "due_at":"2026-08-21T23:59:00","question_count":6,
       "basic_ratio":0.5,"advanced_ratio":0.35}'
```

---

## 5. 已知限制与注意事项

1. **3-2-1 是尽力而为**：当某节经门禁后某层可读题不足时，`assemble` 会回落到
   "从剩余 published 题中补足"，避免出现空卷。例如 §1.1 实测只有 3 道可读题、
   且 `基础=0`，则产出 2 提高 + 1 综合（共 3 题）而非 6 题。**这是数据质量
   问题（8014 该节基础题未被正确 OCR/绑定），不是流水线 bug**——先在 8014 修复
   截图/OCR 再重发。

2. **原题号保留**：`original_no` 取 `questions.source_problem_no`（8014 原始题号）；
   PDF 渲染会先剥离题干自带题号，避免 "10. 10." 重复。

3. **分层会覆盖 8014 的粗标**：8014 常把整节都标为 `综合`；`stratify` 用启发式
   重标以获得可用分布。如要完全尊重 8014 显式难度，需要在同步时记录"是否显式提供"。

4. **依赖**：`assemble` 复用 `D:/workbuddy/2026-08-06-15-31-48/build_worksheet.py`
   的 `WorksheetBuilder`（A4 版面 + 虚线答题留白），需 `PyMuPDF` 与系统宋/黑体字体。

5. **服务进程**：8000 用 `envs\default` 启动
   (`uvicorn app.main:app --host 127.0.0.1 --port 8000`)；8014（证据库）与
   18080（Qwen VLM）必须可达。改代码后需重启 8000。
