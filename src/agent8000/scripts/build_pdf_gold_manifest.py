"""Export teacher-confirmed PDF question regions into a private gold manifest."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    db.row_factory = sqlite3.Row
    cases = []
    jobs = list(db.execute(
        """SELECT g.submission_id,g.result_json,s.file_path
           FROM grading_jobs g JOIN submissions s ON s.id=g.submission_id
           WHERE g.status='completed' AND g.result_json IS NOT NULL"""
    ).fetchall())
    jobs.extend(db.execute(
        """SELECT e.submission_id,e.evidence_json AS result_json,s.file_path
           FROM grading_experiences e JOIN submissions s ON s.id=e.submission_id
           WHERE e.evidence_json IS NOT NULL"""
    ).fetchall())
    for job in jobs:
        try:
            payload = json.loads(job["result_json"])
        except (TypeError, ValueError):
            continue
        for item in payload.get("results", []):
            decision = item.get("teacher_decision") or {}
            confirmed_at = decision.get("confirmed_at")
            if not confirmed_at:
                continue
            region = item.get("teacher_page_mapping") or {}
            required_region = {key: region.get(key) for key in ("page_no", "x", "y", "width", "height")}
            if any(value is None for value in required_region.values()):
                continue
            score = float(decision.get("score", item.get("score") or 0))
            maximum = float(item.get("max_score") or 0)
            expected = "zero" if score <= 0 else ("accept" if maximum and score >= maximum - 0.001 else "review")
            cases.append({
                "id": f"submission-{job['submission_id']}-question-{item.get('question_id')}-part-{item.get('subpart_no') or 'all'}",
                "category": "teacher_confirmed_pdf",
                "expected": expected,
                "problem_no": item.get("problem_no"),
                "source_pdf": job["file_path"],
                "region": required_region,
                "teacher_score": score,
                "max_score": maximum,
                "teacher_labelled_at": confirmed_at,
            })
    output = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "cases": cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "case_count": len(cases)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
