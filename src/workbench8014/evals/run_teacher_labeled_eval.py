"""Run the versioned, teacher-labeled regression benchmark.

The evaluator is deliberately deterministic: it tests the symbolic verifier and
routing contract against teacher-approved labels, validates data governance, and
fails the process when a safety threshold regresses.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.schemas import MisconceptionDiagnosisInput, SymbolicVerificationInput
from skills.misconception_diagnosis import misconception_diagnosis
from skills.symbolic_verification import symbolic_verification

CASES = Path(__file__).with_name("teacher_labeled_cases.jsonl")
THRESHOLDS = Path(__file__).with_name("teacher_label_thresholds.json")
LABEL_VERSION = "teacher-label-v1"
REQUIRED_TOP_LEVEL = {
    "id", "source", "consent_or_anonymization", "teacher_verified",
    "student_answer", "standard_answer", "label_version", "label_status",
    "evaluation_split", "question_type", "data_governance", "teacher_label",
}
REQUIRED_LABEL = {"correct", "expected_route", "expected_diagnosis", "requires_teacher_review"}


def route(confidence: float | None) -> str:
    return "diagnose_misconception" if (confidence or 0.0) >= 0.85 else "independent_solve"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def governance_error(case: dict[str, Any]) -> str | None:
    missing = REQUIRED_TOP_LEVEL - set(case)
    if missing:
        return "missing fields: " + ", ".join(sorted(missing))
    if case["consent_or_anonymization"] != "anonymized":
        return "case is not anonymized"
    if case["teacher_verified"] is not True:
        return "teacher_verified must be true"
    if case["label_version"] != LABEL_VERSION or case["label_status"] != "verified":
        return "label version or status is invalid"
    governance = case["data_governance"]
    if not isinstance(governance, dict) or governance.get("anonymized") is not True:
        return "data_governance.anonymized must be true"
    if governance.get("contains_raw_student_image") is not False:
        return "raw student images are forbidden in the evaluation set"
    if governance.get("approved_for_regression") is not True:
        return "case is not approved for regression"
    label = case["teacher_label"]
    if not isinstance(label, dict) or REQUIRED_LABEL - set(label):
        return "teacher_label is incomplete"
    if not isinstance(label["correct"], bool):
        return "teacher_label.correct must be boolean"
    if label["expected_route"] not in {"diagnose_misconception", "independent_solve"}:
        return "teacher_label.expected_route is invalid"
    if not isinstance(label["requires_teacher_review"], bool):
        return "teacher_label.requires_teacher_review must be boolean"
    return None


def evaluate(cases_path: Path = CASES, thresholds_path: Path = THRESHOLDS) -> dict[str, Any]:
    cases = load_jsonl(cases_path)
    thresholds = json.loads(thresholds_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    governance_errors: list[str] = []
    correct_matches = route_matches = diagnosis_total = diagnosis_matches = 0
    incorrect_total = false_positive_count = 0
    by_type: Counter[str] = Counter()

    for case in cases:
        identifier = str(case.get("id", "unknown"))
        error = governance_error(case)
        if error:
            governance_errors.append(f"{identifier}: {error}")
            continue

        by_type[str(case["question_type"])] += 1
        label = case["teacher_label"]
        verification = symbolic_verification(
            SymbolicVerificationInput(
                student_answer=case["student_answer"],
                standard_answer=case["standard_answer"],
            )
        )
        predicted_correct = bool(verification.correct)
        if predicted_correct == label["correct"]:
            correct_matches += 1
        else:
            failures.append(f"{identifier}: correctness mismatch")
        if label["correct"] is False:
            incorrect_total += 1
            if predicted_correct:
                false_positive_count += 1
                failures.append(f"{identifier}: unsafe false positive")

        predicted_route = route(verification.confidence)
        if predicted_route == label["expected_route"]:
            route_matches += 1
        else:
            failures.append(f"{identifier}: route mismatch")

        expected_diagnosis = label["expected_diagnosis"]
        if expected_diagnosis:
            diagnosis_total += 1
            diagnosis = misconception_diagnosis(
                MisconceptionDiagnosisInput(
                    student_answer=case["student_answer"],
                    standard_answer=case["standard_answer"],
                    problem_text=case.get("problem_text", ""),
                    verification_correct=verification.correct,
                )
            )
            if expected_diagnosis in {item.code for item in diagnosis.diagnoses}:
                diagnosis_matches += 1
            else:
                failures.append(f"{identifier}: diagnosis mismatch")

    valid_count = len(cases) - len(governance_errors)
    correctness_accuracy = correct_matches / valid_count if valid_count else 0.0
    route_accuracy = route_matches / valid_count if valid_count else 0.0
    false_positive_rate = false_positive_count / incorrect_total if incorrect_total else 0.0
    diagnosis_accuracy = diagnosis_matches / diagnosis_total if diagnosis_total else None

    threshold_failures: list[str] = []
    if len(cases) < thresholds["min_case_count"]:
        threshold_failures.append("sample count below threshold")
    if correctness_accuracy < thresholds["min_correctness_accuracy"]:
        threshold_failures.append("correctness accuracy below threshold")
    if route_accuracy < thresholds["min_route_accuracy"]:
        threshold_failures.append("route accuracy below threshold")
    if false_positive_rate > thresholds["max_false_positive_rate"]:
        threshold_failures.append("false positive rate above threshold")
    if len(governance_errors) > thresholds["max_governance_errors"]:
        threshold_failures.append("data governance errors above threshold")

    report = {
        "benchmark": LABEL_VERSION,
        "case_count": len(cases),
        "valid_case_count": valid_count,
        "coverage_by_question_type": dict(sorted(by_type.items())),
        "correctness_accuracy": correctness_accuracy,
        "route_accuracy": route_accuracy,
        "false_positive_count": false_positive_count,
        "false_positive_rate": false_positive_rate,
        "diagnosis_label_count": diagnosis_total,
        "diagnosis_accuracy": diagnosis_accuracy,
        "governance_error_count": len(governance_errors),
        "governance_errors": governance_errors,
        "case_failures": failures,
        "threshold_failures": threshold_failures,
        "passed": not failures and not governance_errors and not threshold_failures,
        "thresholds": thresholds,
    }
    return report


def run(report_path: str | Path | None = None) -> int:
    report = evaluate()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if report_path:
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", help="write the JSON report to this path")
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.report))
