"""Render anchored Route2 evidence and stage non-destructive LaTeX review candidates.

Without an explicitly reachable recognizer this tool deliberately leaves
``latex_candidate`` blank.  A source crop is evidence, not a licence to infer
superscripts, inequalities, or other mathematical symbols from noisy text.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import fitz
import httpx


def clip_with_padding(rect: fitz.Rect, page: fitz.Page, padding: float = 10) -> fitz.Rect:
    return (rect + (-padding, -padding, padding, padding)) & page.rect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--vlm-url", default="", help="Optional /solve-from-image base URL; never writes the question bank")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT a.id AS anchor_id,a.problem_id,a.pdf_page_index,a.bbox_json,a.confidence,
                   s.section_no,p.problem_no,p.sub_no
            FROM problem_source_anchors a
            JOIN problems p ON p.id=a.problem_id JOIN sections s ON s.id=p.section_id
            WHERE a.status='candidate' AND a.resolution_method LIKE 'question_number%native_text'
            ORDER BY s.section_no,CAST(p.problem_no AS INTEGER),p.sub_no
        """).fetchall()
    finally:
        conn.close()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(args.pdf); output = []
    try:
        for row in rows:
            bbox = json.loads(row["bbox_json"])
            page = document[int(row["pdf_page_index"])]
            clip = clip_with_padding(fitz.Rect(*bbox), page)
            filename = f"{row['section_no']}-{row['problem_no']}-a{row['anchor_id']}.png"
            crop_path = args.out_dir / filename
            page.get_pixmap(matrix=fitz.Matrix(args.dpi / 72, args.dpi / 72), clip=clip, alpha=False).save(crop_path)
            raw_text = page.get_textbox(clip).strip()
            recognition: dict = {}
            error = ""
            if args.vlm_url:
                try:
                    payload = {"image_base64": base64.b64encode(crop_path.read_bytes()).decode("ascii"),
                               "section_no": row["section_no"], "problem_no": str(row["problem_no"])}
                    with httpx.Client(timeout=180) as client:
                        response = client.post(args.vlm_url.rstrip("/") + "/solve-from-image", json=payload)
                        response.raise_for_status(); recognition = response.json()
                except Exception as exc:  # preserve evidence even when the remote model fails
                    error = str(exc)
            output.append({
                "candidate_key": f"route2-anchor-{row['anchor_id']}", "anchor_id": row["anchor_id"],
                "problem_id": row["problem_id"], "section_no": row["section_no"],
                "problem_no": row["problem_no"], "sub_no": row["sub_no"] or "",
                "pdf_page_index": row["pdf_page_index"], "pdf_page_number": row["pdf_page_index"] + 1,
                "bbox_pdf_points": [round(value, 2) for value in clip],
                "evidence_crop": crop_path.name, "raw_native_text": raw_text,
                "raw_native_text_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                "latex_candidate": str(recognition.get("problem_text") or ""),
                "model_result": recognition, "recognizer_error": error,
                "state": "needs_teacher_review" if recognition else "awaiting_recognizer",
                "teacher_action": "逐项对照原图确认模型转写；不得把候选自动写回题库。",
                "anchor_confidence": row["confidence"],
            })
    finally:
        document.close()
    manifest = {"schema_version": "latex-review-candidates/v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "recognizer_called": bool(args.vlm_url), "reason": "VLM candidate only; no question-bank write", "dpi": args.dpi,
                "count": len(output), "candidates": output}
    (args.out_dir / "latex_review_candidates.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"staged": len(output), "recognizer_called": bool(args.vlm_url),
                      "with_latex_candidate": sum(bool(item["latex_candidate"]) for item in output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
