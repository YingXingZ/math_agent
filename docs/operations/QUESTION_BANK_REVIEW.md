# 题库复核与重新审核 SOP

## 安全状态机

```text
原始裁图 -> VLM 识别候选 -> pending（教师复核） -> approved / rejected
                                             |
                                             +-> 未批准：不写 8014、不进入 Agent 题库、不发布
```

唯一允许写回权威题库的动作是教师对 `ai_stem_candidates` 的显式批准。候选识别、人工编辑和拒绝均不会自动向学生发布。

## 17 条已有裁图

1. 生成只读清单：

   ```powershell
   python src/tools/question_bank_readiness.py --image-root D:\workbuddy\2026-08-06-15-31-48\extract_img --out $env:TEMP\question-bank-readiness.json
   ```

2. 先执行一条 dry-run，核对远端 VLM、裁图和题号对应正确：

   ```powershell
   python src/tools/stage_image_review_candidates.py --manifest $env:TEMP\question-bank-readiness.json --vlm-url http://<VLM_HOST>:18080 --limit 1
   ```

3. 取得教材裁图可发送至 VLM 的授权后，再暂存全部候选。`--stage` 只写本地 Agent 数据库的 `pending` 队列；不修改 8014：

   ```powershell
   python src/tools/stage_image_review_candidates.py --manifest $env:TEMP\question-bank-readiness.json --vlm-url http://<VLM_HOST>:18080 --stage
   ```

4. 教师逐条打开原始裁图，校对题干、标准答案、解答和置信度。编辑正确后调用现有批准接口；不正确则拒绝。批准动作会先写 8014，成功后才同步 Agent 缓存。

5. 生成队列对账报告，避免遗漏未识别或已有决定的条目：

   ```powershell
   python src/tools/review_queue_report.py --manifest $env:TEMP\question-bank-readiness.json --out $env:TEMP\review-queue-report.json
   ```

## 92 条缺少源图

这些题目不具备可核对的原始证据，不能用 OCR 文本猜测补全。对每条记录：

1. 从教材或答案 PDF 定位原页；
2. 裁出单题图并存至 `IMAGE_ROOT/book/<章节>/`；
3. 更新/绑定该题的 `crop_image_path`；
4. 重新生成 readiness 清单；
5. 只有被列为 `ready_for_teacher_review` 后，才按 17 条流程识别和复核。

## 73 条疑似乱码

1. 保持 `blocked` 或待复核状态，禁止同步到可发布题库；
2. 对有裁图的记录，以原图为准重新识别，不以乱码字段为准；
3. 对无裁图记录，按“92 条缺少源图”补证据；
4. 教师复核时至少检查题号、题干、符号/上下标、标准答案和完整解答；
5. 批准后运行 `bulk_sync_8014.py`，再执行 `scan_corrupt_8014.py` 与 pytest 回归；只有损坏门禁通过的题目才可进入发布筛选。
