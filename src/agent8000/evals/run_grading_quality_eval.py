from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.grading_pipeline import _completion_check, _math_equal, _recognition_is_contaminated

CASES_PATH = Path(__file__).with_name("grading_quality_cases.json")


def classify_case(case: dict[str, Any]) -> tuple[str, list[str]]:
    if case.get("answer_present") is False:
        return "zero", []

    reasons: list[str] = []
    if case.get("answer_present") is not True:
        reasons.append("answer_presence_unknown")

    model_result = {
        "work_complete": case.get("work_complete"),
        "correct": case.get("model_correct"),
        "completion_evidence": case.get("completion_evidence", ""),
    }
    completion = _completion_check(str(case.get("recognized_work") or ""), model_result)
    if not completion["complete"]:
        reasons.append("incomplete")

    recognized = str(case.get("recognized_work") or "")
    standard = str(case.get("standard_answer") or "")
    if _recognition_is_contaminated(recognized, standard):
        reasons.append("reference_contamination")

    equivalence = _math_equal(recognized, standard)
    if (
        case.get("model_correct") is True
        and equivalence.get("available")
        and equivalence.get("equal") is False
        and float(equivalence.get("confidence") or 0) >= 0.85
    ):
        reasons.append("math_contradiction")

    if case.get("model_correct") is not True:
        reasons.append("not_confirmed_correct")
    if float(case.get("confidence") or 0) < 0.85:
        reasons.append("low_confidence")
    if case.get("need_review"):
        reasons.append("model_requested_review")

    return ("review", list(dict.fromkeys(reasons))) if reasons else ("accept", [])


def run_eval(cases_path: Path = CASES_PATH) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        predicted, reasons = classify_case(case)
        rows.append({
            "id": case["id"], "category": case["category"],
            "expected": case["expected"], "predicted": predicted,
            "passed": predicted == case["expected"], "reasons": reasons,
        })

    expected_safe = [row for row in rows if row["expected"] != "accept"]
    expected_accept = [row for row in rows if row["expected"] == "accept"]
    false_accepts = [row for row in expected_safe if row["predicted"] == "accept"]
    false_reviews = [row for row in expected_accept if row["predicted"] != "accept"]
    passed = sum(row["passed"] for row in rows)
    return {
        "case_count": len(rows),
        "passed": passed,
        "accuracy": passed / len(rows) if rows else 0.0,
        "false_accept_rate": len(false_accepts) / len(expected_safe) if expected_safe else 0.0,
        "false_review_rate": len(false_reviews) / len(expected_accept) if expected_accept else 0.0,
        "category_counts": dict(Counter(row["category"] for row in rows)),
        "failures": [row for row in rows if not row["passed"]],
        "rows": rows,
    }


def main() -> int:
    report = run_eval()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (
        report["accuracy"] == 1.0
        and report["false_accept_rate"] == 0.0
        and report["false_review_rate"] == 0.0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
