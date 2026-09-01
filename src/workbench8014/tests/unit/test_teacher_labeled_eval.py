from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_teacher_labeled_eval import CASES, evaluate, run


def test_teacher_labeled_cases_are_governed_and_baseline_passes(capsys, tmp_path):
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(cases) == 30
    assert all(case["teacher_verified"] is True for case in cases)
    assert all(case["data_governance"]["contains_raw_student_image"] is False for case in cases)

    report = evaluate()
    assert report["passed"] is True
    assert report["correctness_accuracy"] == 1.0
    assert report["route_accuracy"] == 1.0
    assert report["false_positive_rate"] == 0.0

    output = tmp_path / "teacher_eval_report.json"
    assert run(output) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert '"case_count": 30' in capsys.readouterr().out
