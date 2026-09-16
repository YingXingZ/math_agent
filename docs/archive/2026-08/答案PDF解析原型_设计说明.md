# 答案 PDF 解析原型 — 设计说明

> 对应需求: **上传答案 PDF → MinerU 解析 → 找到 1.1 节 → 输出结构化 JSON → 暂时不入正式数据库**

## 1. 关键事实(实测)

对 4 本答案 PDF 逐页扫描文字层:

| PDF | 页数 | 文字层页 | 扫描页 |
|---|---|---|---|
| 同济高数8版-答案-上册 | 395 | 0 | 395 |
| 同济高数8版-答案-下册 | 331 | 0 | 331 |
| 李继成高数-答案-上册 | 317 | 0 | 317 |
| 李继成高数-答案-下册 | 303 | 0 | 303 |

**结论: 4 本答案书 100% 是扫描版(图片 PDF), 不含可选文字层。**
因此:
- PyMuPDF 的 `get_text()` 直接返回空 → **纯文本抽取不可用**。
- **MinerU(扫描版 OCR + 版面 + 公式识别)是必需解析器**, 不是可选项。这也印证了当初选择 MinerU 的思路。

## 2. 流水线架构

```
上传 PDF
   │
   ▼
[解析器层 · 可插拔]
   ├─ MinerUParser   (扫描版必需; 调 mineru CLI, 读 *_middle.json → Span[页码,文本,bbox])
   └─ PyMuPDFParser  (仅当 PDF 带文字层时自动回退, 更快)
   │
   ▼
[小节定位]  locate_section()  — 正则 `习题\s*(\d+)\s*[-.．]\s*(\d+)`
   │          归一化 "1.1"/"1-1"/"习题 1-1" → "1-1"; 取目标节到下一节之间的文本区间
   ▼
[题目抽取]  extract_problems() — 题号行 `^\s*(\d{1,3})\s*[\.．、)](?=\s|$)` (排除 "12.5" 小数)
   │          收集每题原始行, 收尾时按 "(n)" 切成小题
   ▼
[草稿输出]  drafts/<pdf名>_sec-<节>.json   ← 不连接任何数据库
```

## 3. 输出 JSON Schema

```json
{
  "meta": {
    "source_pdf": "...",
    "source_pdf_name": "同济高数8版-答案-上册(1).pdf",
    "parser": "mineru",
    "target_section": "1-1",
    "section_title": "习题 1-1",
    "found": true,
    "page_span": [12, 15],
    "problem_count": 18,
    "generated_at": "2026-08-14T...",
    "note": "DRAFT — 未写入正式数据库"
  },
  "problems": [
    {
      "problem_no": "1",
      "answer_text": "（整题答案，若含小题则为空）",
      "sub_items": [ { "sub_no": "1", "answer_text": "..." } ],
      "source_page": 12,
      "bbox": [x0, y0, x1, y1]
    }
  ]
}
```

字段说明:
- `problem_no`: 顶层题号(1,2,3…)
- `sub_items`: 小题列表, 按 `(1)(2)(3)` 切分
- `answer_text`: 整题答案文本; 若题目只含小题则为空
- `source_page` / `bbox`: 该题在 PDF 中的页码与包围盒(PDF 点坐标), 供后续人工复核/裁剪

## 4. 如何运行

```bash
# 依赖(已装 pymupdf 进 pdfpipe venv; mineru 装进 mineruenv venv)
./pdfpipe/Scripts/python answer_pdf_mineru_pipeline.py \
    --pdf "D:/My File/大四/高数教材答案/同济高数8版-答案-上册(1).pdf" \
    --section 1-1 --pages 60

# 或启动上传服务: python answer_upload_server.py  (POST /upload-answer-pdf)
```

`--pages 60` 只解析前 60 页(§1.1 在书前部), 大幅加快演示; 正式跑可去掉。

## 5. 与正式流程的边界

- 本原型**全程不 import sqlite / 不连 8014 / 不写 api.workbench.db**。
- 草稿 JSON 落 `drafts/`, 由阶段 C/D 的 Agent(人工复核 + 数学判等)决定如何入库。
- 公式保真度依赖 MinerU; PyMuPDF 兜底仅用于带文字层的数字 PDF。

## 6. 下一步

1. MinerU 安装完成后, 实跑前 60 页, 校验 §1.1 抽取质量, 调正则。
2. 公式图块当前以 `<formula>` 占位; 后续可接入公式 OCR / LaTeX 还原。
3. 批量: 遍历所有小节 → 生成整本书草稿, 再与 8014 题库按 `section_no + problem_no` 自动匹配(复用阶段 B 思路)。
