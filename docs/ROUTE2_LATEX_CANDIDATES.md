# Route2 教师确认 LaTeX 候选

`stage_route2_latex_candidates.py` 只处理已通过 1B 门禁的 `candidate` 锚点。它从登记 PDF 按 bbox 以 400 DPI 裁出证据图，并保留原生文本与哈希；不会更新 `problems`、`questions`、`published` 或 `blocked`。

```powershell
python src/tools/stage_route2_latex_candidates.py --db api.workbench.db --pdf 'E:\xwechat_files\wxid_l7836vvhxpdh11_4125\msg\file\2026-08\李继成高数-答案-下册-OCR.pdf' --out-dir answer_source_previews/route2-latex-candidates
```

若没有已配置且可访问的公式/视觉识别器，`latex_candidate` 必须保持为空，状态为 `awaiting_recognizer`。这是刻意的安全门禁：不能从噪声 OCR 推断上标、积分限、不等号或分式。教师应在原图上确认模型随后产生的候选，而不是把该阶段的原生文本直接写回题库。
