# 批量 PDF 取证 → LaTeX 候选 → 教师确认

此闭环证据优先：不会从乱码猜数学内容，也不会自动发布。

1. 生成只读清单：

   ```powershell
   $py='C:/Users/YXZ/.workbuddy/binaries/python/envs/default/Scripts/python.exe'
   & $py src/tools/build_live_pdf_evidence_plan.py --out docs/live_pdf_evidence_plan.json
   ```

   `ready_for_teacher_review` 已有单题原图，才能进入 VLM；`ready_for_pdf_page` 有 PDF 和页码，仍需先人工裁单题图；其余状态必须补来源或 IMA 授权。

2. 渲染已登记原始页（先 dry-run；`--render` 只写 PNG，不写数据库）：

   ```powershell
   & $py src/tools/render_pdf_evidence_pages.py --manifest docs/live_pdf_evidence_plan.json --out-dir evidence_pages
   & $py src/tools/render_pdf_evidence_pages.py --manifest docs/live_pdf_evidence_plan.json --out-dir evidence_pages --render
   ```

3. VLM SSH 隧道可用后，只对已有单题图暂存候选。默认不写；`--stage` 只写本地 `ai_stem_candidates` 的 `pending`：

   ```powershell
   & $py src/tools/stage_image_review_candidates.py --manifest docs/live_pdf_evidence_plan.json --limit 10
   & $py src/tools/stage_image_review_candidates.py --manifest docs/live_pdf_evidence_plan.json --limit 10 --stage
   ```

4. 在 8000「AI 题干候选复核」对照原图逐题确认；只有确认才写回 8014 和缓存。VLM、页码或 IMA 资料缺失时停在对应状态，不能生成或发布候选。
