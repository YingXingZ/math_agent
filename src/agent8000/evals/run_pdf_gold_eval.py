"""Validate the private, teacher-labelled PDF grading gold set."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_LABELS = {"accept", "review", "zero"}


def evaluate(path: Path, *, require_files: bool = True) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", payload if isinstance(payload, list) else [])
    failures = []
    categories = set()
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or f"row-{index + 1}")
        categories.add(str(case.get("category") or ""))
        expected = case.get("expected")
        if expected not in REQUIRED_LABELS:
            failures.append({"id": case_id, "reason": "invalid_expected"})
        if not case.get("problem_no"):
            failures.append({"id": case_id, "reason": "missing_problem_no"})
        if not case.get("source_pdf"):
            failures.append({"id": case_id, "reason": "missing_source_pdf"})
        elif require_files and not Path(case["source_pdf"]).exists():
            failures.append({"id": case_id, "reason": "source_pdf_not_found"})
        region = case.get("region") or {}
        if not all(key in region for key in ("page_no", "x", "y", "width", "height")):
            failures.append({"id": case_id, "reason": "missing_crop_region"})
        if not case.get("teacher_labelled_at"):
            failures.append({"id": case_id, "reason": "missing_teacher_label"})
    return {"case_count": len(cases), "category_count": len(categories - {""}), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--min-cases", type=int, default=100)
    parser.add_argument("--min-categories", type=int, default=6)
    parser.add_argument("--schema-only", action="store_true")
    args = parser.parse_args()
    report = evaluate(args.manifest, require_files=not args.schema_only)
    report["passed"] = not report["failures"] and report["case_count"] >= args.min_cases and report["category_count"] >= args.min_categories
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
