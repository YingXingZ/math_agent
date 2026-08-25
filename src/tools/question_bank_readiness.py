"""Build a read-only review-readiness manifest for incomplete 8014 problems.

The manifest never edits the database or publishes a question.  It identifies
which records have a locally available crop and therefore can be sent through
the existing VLM -> teacher-review pipeline, versus records that need an
original textbook/answer source first.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from tool_config import image_root, workbench_db


FULLWIDTH_GARBAGE = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A\uFF5E\uFF07\uFF3C\uFF5C\uE000-\uF8FF\uFFFD]")


def looks_corrupt(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    return "�" in text or len(FULLWIDTH_GARBAGE.findall(text)) / len(text) > 0.006


def resolve_crop(root: Path, stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    candidate = root / stored_path.replace("\\", "/")
    return candidate if candidate.is_file() else None


def build_manifest(db_path: Path, crop_root: Path) -> dict:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT p.id, p.problem_no, p.sub_no, p.ptype, p.difficulty, p.content_text, p.std_answer,
                  p.answer_status, p.crop_image_path, s.section_no
           FROM problems p JOIN sections s ON s.id=p.section_id
           ORDER BY s.section_no, p.problem_no, p.sub_no"""
    ).fetchall()
    connection.close()

    candidates = []
    for row in rows:
        stem = (row["content_text"] or "").strip()
        answer = (row["std_answer"] or "").strip()
        corrupt = row["answer_status"] == "corrupt_ocr" or looks_corrupt(stem) or looks_corrupt(answer)
        incomplete = not stem or not answer
        if not corrupt and not incomplete:
            continue
        crop = resolve_crop(crop_root, row["crop_image_path"])
        reason = "corrupt" if corrupt else "missing_fields"
        disposition = "ready_for_teacher_review" if crop else "requires_source_image"
        candidates.append({
            "problem_id": row["id"],
            "section_no": row["section_no"],
            "problem_no": row["problem_no"],
            "sub_no": row["sub_no"] or "",
            "ptype": row["ptype"] or "calc",
            "difficulty": row["difficulty"],
            "reason": reason,
            "missing": [name for name, value in (("content_text", stem), ("std_answer", answer)) if not value],
            "crop_image_path": row["crop_image_path"] or "",
            "crop_available": bool(crop),
            "disposition": disposition,
        })
    counts = Counter(item["disposition"] for item in candidates)
    counts.update({"complete": len(rows) - len(candidates), "total": len(rows)})
    return {"database": str(db_path), "image_root": str(crop_root), "counts": dict(counts), "candidates": candidates}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a read-only question-bank review manifest")
    parser.add_argument("--db", type=Path, default=workbench_db())
    parser.add_argument("--image-root", type=Path, default=image_root())
    parser.add_argument("--out", type=Path, required=True, help="JSON output path")
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"Workbench database not found: {args.db}")
    manifest = build_manifest(args.db, args.image_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False, sort_keys=True))
    print(f"Wrote review manifest: {args.out}")


if __name__ == "__main__":
    main()
