# Route 2 — 脚本化章节导入指南

> 适用场景：教材某章节在 8014 证据库中**既无标准答案、也无裁切图**（例如早期 §5.1）。
> 这类章节无法走「答案书 OCR → 双模型校验」的常规通道（Route 1），需要用本脚本
> 由教师/助教提供的**原题裁切图**重建可信题目，再补回证据库。

## 1. 原则（红线）

- **绝不伪造数学内容**：教材原页裁切图与对应答案必须由教师/助教提供；脚本只负责
  「识别 → 结构化 → 入库」的自动化管道。
- 若某题缺答案，导入后标记 `unverified`（待人工复核），**绝不以猜测值发布**。
- 每道导入题都保留源裁切图于 `answer_source_previews/route2/<章节>/` 供教师溯源。

## 2. 前置条件

- 远端 VLM（`/solve-from-image`）在线：`curl -s http://127.0.0.1:18080/health`。
- 本地 8014 工作台在线（默认 `http://127.0.0.1:8014/api`）。
- 运行环境：本项目 venv
  `C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （或任意装有 `httpx` 的 Python 3.11）。

## 3. 输入目录布局

```
route2_input/<章节>/
├── crops/
│   ├── <章节>-<题号>.png          # 每图 = 一题；可含小问
│   ├── <章节>-<题号>-<小问>.png   # 例：5.1-1-2.png → 第1题第2小问
│   └── ...
└── manifest.json                  # 可选：人工录入的答案 / 知识点覆盖
```

题号从文件名自动推断：`5.1-1.png → (problem_no="1", sub_no="")`；
`5.1-1-2.png → ("1","2")`。`manifest.json` 中的键用**裁切图文件名**（含扩展名）对应。

### manifest.json 结构

```json
{
  "chapter_title": "第五章 第一节 向量及其线性运算",
  "knowledge_pts": ["向量线性运算", "向量概念"],
  "problems": {
    "5.1-1.png": {
      "problem_no": "1",
      "sub_no": "",
      "ptype": "calc",
      "difficulty": 3,
      "knowledge_pts": ["向量线性运算"],
      "content_text": "（可选）人工录入题干，优先于 VLM 识别",
      "grading_steps": "（可选）评分步骤",
      "std_answer": "（可选）人工录入标准答案；缺则标记 unverified"
    }
  }
}
```

字段说明：
- `ptype`：`calc` / `proof`（与 `db.normalize_question_type` 对齐）。
- `difficulty`：整数（1 基础 / 2 提高 / 3 综合，默认 3）。
- 仅 `std_answer` 非空时该题才算「有答案」；否则进入待复核队列。

## 4. 运行

```bash
# 从 高数作业助手 目录执行
VENV=C:/Users/YXZ/.workbuddy/binaries/python/envs/default/Scripts/python.exe

# 1) 试运行：只识别 + 生成 JSON，不写入 8014
$VENV route2_chapter_importer.py --chapter 5.1 --input route2_input/5.1 --dry-run

# 2) 正式导入：生成 JSON 并写入本地 8014 证据库（含源图证据）
$VENV route2_chapter_importer.py --chapter 5.1 --input route2_input/5.1 --push
```

- 产物：`ingest/route2_<章节>.json`（供审计/复用）。
- `--push` 走 8014 既有 `POST /ingest/book`，与常规 extract_book 管道一致。

## 5. 导入后

1. 在 8014 工作台确认章节题目与裁切图落地。
2. 在 Agent（8000）触发同步：教师端「AI 题干候选复核」面板会列出新题候选。
3. 教师勾选「批准并写回」→ 写回 8014 `content_text` + 本地 `questions` 发布。
4. 缺答案的题保持 `unverified`，待教师补充答案后再发布。

## 6. 已用示例

- `ingest/route2_5_1.json` ~ `route2_5_6.json`：§5.1–§5.6 已据此脚本化导入并发布
  （共 89 题进入本地缓存）。
- 模板脚手架见 `route2_input/_TEMPLATE/`。
