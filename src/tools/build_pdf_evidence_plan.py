"""Create a read-only PDF-source evidence plan for missing/corrupt 8014 rows.

The output deliberately leaves page numbers blank.  It gives a teacher a
stable row identifier, recommended source PDF, and intended crop location; it
never edits SQLite, copies PDFs, crops images, or calls the VLM.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path

from question_bank_readiness import looks_corrupt, resolve_crop
from tool_config import workbench_db
from pathlib import Path as _Path
import sys

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from agent8000.app.question_bank_review import _scan_looks_corrupt


def _volume(section_no: str) -> str:
    try:
        return "下册" if int(section_no.split(".", 1)[0]) >= 8 else "上册"
    except ValueError:
        return "待确认"


def _pdf_label(volume: str, field: str) -> str:
    kind = "答案" if field in {"std_answer", "corrupt"} else "教材"
    return f"李继成高数-{kind}-{volume}(1).pdf" if volume != "待确认" else "待人工选择 PDF"


def build_rows(db: Path, image_root: Path) -> list[dict[str, str]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id,p.problem_no,p.sub_no,p.content_text,p.std_answer,p.answer_status,
                  p.crop_image_path,s.section_no
           FROM problems p JOIN sections s ON s.id=p.section_id
           ORDER BY s.section_no,p.problem_no,p.sub_no"""
    ).fetchall()
    conn.close()
    plan: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        stem, answer = (row["content_text"] or "").strip(), (row["std_answer"] or "").strip()
        missing = []
        if not stem:
            missing.append("content_text")
        if not answer:
            missing.append("std_answer")
        # Missing-source queue follows the existing readiness rule; the separate
        # 73-row corruption queue follows the historic content-only scan.
        incomplete_or_corrupt = row["answer_status"] == "corrupt_ocr" or looks_corrupt(stem) or looks_corrupt(answer) or bool(missing)
        missing_source = incomplete_or_corrupt and not resolve_crop(image_root, row["crop_image_path"])
        corrupt = bool(stem and answer and _scan_looks_corrupt(stem))
        kinds = (["缺源图"] if missing_source else []) + (["疑似乱码"] if corrupt else [])
        for kind in kinds:
            key = (str(row["id"]), kind)
            if key in seen:
                continue
            seen.add(key)
            source_field = "corrupt" if kind == "疑似乱码" else ("std_answer" if "std_answer" in missing else "content_text")
            volume = _volume(row["section_no"])
            plan.append({
                "problem_id": str(row["id"]), "task_type": kind, "section_no": row["section_no"],
                "problem_no": str(row["problem_no"]), "sub_no": str(row["sub_no"] or ""),
                "missing_or_issue": ",".join(missing) if missing else "corrupt_ocr_or_ascii_salad",
                "recommended_pdf": _pdf_label(volume, source_field), "pdf_page": "",
                "crop_output_path": f"book/{row['section_no']}/manual_{row['problem_no']}_{row['sub_no'] or 'main'}.png",
                "teacher_note": "定位页码后裁单题图；绑定 crop_image_path；重生成 readiness 清单",
            })
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only PDF source evidence plan")
    parser.add_argument("--db", type=Path, default=workbench_db())
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"Workbench database not found: {args.db}")
    plan = build_rows(args.db, args.image_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(plan[0]) if plan else ["problem_id"])
        writer.writeheader(); writer.writerows(plan)
    print(f"rows={len(plan)} output={args.out}")


if __name__ == "__main__":
    main()
