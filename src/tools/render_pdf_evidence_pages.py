"""Render registered PDF evidence pages, never automatic question crops."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz


def main() -> None:
    parser = argparse.ArgumentParser(description="Render registered PDF source pages for teacher evidence review")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--render", action="store_true", help="write PNG pages; default is dry-run")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = [item for item in manifest.get("candidates", []) if item.get("disposition") == "ready_for_pdf_page"]
    if args.limit: items = items[:args.limit]
    print(json.dumps({"eligible_pages": len(items), "mode": "render" if args.render else "dry-run"}, ensure_ascii=False))
    if not args.render or not items: return
    args.out_dir.mkdir(parents=True, exist_ok=True); rendered = []; scale = args.dpi / 72
    for item in items:
        pdf, page_no = Path(item["resolved_pdf_path"]), int(item["source_page"])
        if page_no < 1: continue
        doc = fitz.open(pdf)
        try:
            if page_no > doc.page_count: continue
            output = args.out_dir / f"q{item['question_id']}_source-p{page_no}.png"
            doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(output)
            rendered.append({"question_id": item["question_id"], "source_problem_id": item["source_problem_id"], "page_image": str(output), "pdf_page": page_no})
        finally:
            doc.close()
    (args.out_dir / "rendered_evidence.json").write_text(json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rendered": len(rendered), "out_dir": str(args.out_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
