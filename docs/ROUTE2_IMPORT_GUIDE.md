# Route 2 数据导入指南（无答案且无裁切图章节，如 §5.1）

## 背景与目标

§5.1 这类章节在 8014 证据库中**既没有标准答案，也没有裁切图（extract_img/book/ 下仅有 1.1–4.7）**。
Route 2 的目标是把这类章节的教材原页 + 对应答案，**脚本化**导入 8014，补齐知识库，使教师后续可以
组卷、自动批改。

导入器：`D:\workbuddy\2026-08-06-15-31-48\route2_chapter_importer.py`

## 重要：需要你提供什么

> 本脚本**不会伪造任何数学内容**。教材原页（裁切图）与对应答案，必须由你（或助教）提供。
> 脚本只负责"识别 → 结构化 → 入库"的自动化。

你需要准备：

1. **教材原页裁切图**：§5.1 每题一张，放到 `route2_input/5.1/crops/`。
   - 一图一题；含小问的题放同一张图即可（VLM 自动拆小问）。
   - 命名 `5.1-1.png`、`5.1-2.png` ……（数字自动识别为题号）。
2. **（可选）答案 / 知识点覆盖**：在 `route2_input/5.1/manifest.json` 按文件名补充
   `std_answer`、`knowledge_pts`、`content_text` 等。凡 manifest 提供的字段会**覆盖** VLM 识别，
   适合老师/助教人工校对答案。
3. 未提供答案的题会被标记为 `unverified`（待人工复核），绝不以猜测值发布。

## 工作流程

```
crops/*.png  ──┐
manifest.json ─┤──> VLM /solve-from-image（题图转写+独立求解）
               │       │
               │       ▼
               │   book_problems.json（8014 /ingest/book 格式）
               │       │
               └───────┴──> [--push] 写入本地 8014 证据库（含源图证据）
```

- **识别**：对每张裁切图调用 VLM `/solve-from-image`，得到 `problem_text`（题干）与 `std_answer`（答案）。
- **结构化**：按 `manifest.json` 覆盖校对，逐小问展开，生成 8014 `/ingest/book` 所需的 `book_problems.json`
  （textbook → section → problems，含 `content_text`、`std_answer`、`knowledge_pts`、源图 `img` 路径）。
- **入库**：`--push` 时把 JSON 与源裁切图复制到 8014 工作目录（`answer_source_previews/route2/5.1/`），
  再调用 `POST /ingest/book`（与既有 `extract_book.py` 管道完全一致）。

## 运行步骤

```bat
cd D:\workbuddy\2026-08-06-15-31-48

:: 1) 仅识别 + 生成 JSON，不写入 8014（先预览，安全）
python route2_chapter_importer.py --chapter 5.1 --input route2_input/5.1 --dry-run

:: 2) 正式导入（生成 JSON 并写入本地 8014 证据库）
python route2_chapter_importer.py --chapter 5.1 --input route2_input/5.1 --push
```

环境变量（可选）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `MATH_VLM_URL` | `http://127.0.0.1:18080` | VLM 识别服务地址（远程盒子为 `http://222.211.217.7:18080`）|
| `EVIDENCE_DIR` | `D:\My File\大四\高数教材答案` | 8014 工作目录（源图相对它解析）|
| `EVIDENCE_URL` | `http://127.0.0.1:8014` | 8014 入库接口地址 |

> 若 VLM 在远程盒子，请先确认 8014 能访问该盒子，或在能访问两者的机器上运行导入器。

## 校验与质量

- 缺答案的题：`answer_status = unverified`，进入 Route 1 的待复核流程（VLM 识别 → 教师批准写回）。
- 源裁切图作为证据保留在 `answer_source_previews/route2/5.1/`，教师可在 8014 查看原始凭证。
- 建议导入后用 8014 的 `GET /problems?section_no=5.1` 核对题量，并抽查 2–3 题的答案准确性。

## 自检（无需真实 §5.1 材料）

可用本地已存在的 §1.1 裁切图验证整条管道（识别→JSON→schema），不写入 8014：

```bat
python route2_chapter_importer.py --self-test
```
