# 教材来源证据基础（Milestone 1A）

本阶段只登记可追溯的 PDF 元数据、哈希及日后题目页锚点的表结构。它不会调用 OCR/VLM、不会改写题目文本，也不会变更 `published`/`blocked` 状态。

## 已确认的映射

- `李继成高数-教材-上册-2版(1).pdf` 对应教材记录 `7f55d4ae-94e7-4428-b2bb-da2e113070c4`。
- Route2 的 §5.1–§5.6 六个虚拟教材记录共用用户确认的原始资料：`李继成高数-答案-下册-OCR.pdf`。该资料在登记时以 SHA-256 验证，而非信任机器绝对路径。
- 可复跑清单见 [textbook_document_registration.json](textbook_document_registration.json)。SQLite 中只保存相对路径、哈希和探测结果；原始 PDF 不入库。

## 操作

先配置根目录（Windows 以分号分隔）：

```powershell
$env:SOURCE_DOCUMENT_ROOT = 'D:\My File\大四\高数教材答案;E:\xwechat_files\wxid_l7836vvhxpdh11_4125\msg\file\2026-08'
```

先做不写库核验：

```powershell
python src/tools/register_textbook_documents.py --db api.workbench.db --mapping docs/textbook_document_registration.json --source-root 'D:\My File\大四\高数教材答案' --source-root 'E:\xwechat_files\wxid_l7836vvhxpdh11_4125\msg\file\2026-08' --dry-run
```

核验无误后移除 `--dry-run` 执行登记。该命令会自动创建 `textbook_documents`、`problem_source_anchors` 及索引；8014 API 初始化时也会幂等创建同一结构。

只读盘点命令：

```powershell
python src/tools/source_document_inventory.py --file 'D:\My File\大四\高数教材答案\李继成高数-教材-上册-2版(1).pdf' --file 'E:\xwechat_files\wxid_l7836vvhxpdh11_4125\msg\file\2026-08\李继成高数-答案-下册-OCR.pdf' --out docs/source_evidence_inventory.json
```

风险队列仅做快照、不会把候选当作已证实错误：

```powershell
python src/tools/snapshot_risk_queue.py --agent-db src/agent8000/data/homework.db --workbench-db api.workbench.db --out docs/risk_snapshots/current.json
```

## 边界与下一步

现有旧裁切图只能证明部分答案来源；缺失裁切图不能自动反推题页。Milestone 1B 应只对已取得页码/框选证据的单题建立 `problem_source_anchors`，再生成“教师确认的 LaTeX 候选”；仍不得由乱码推测数学内容。
