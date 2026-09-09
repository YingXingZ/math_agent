from app.grading_pipeline import _all_rubric_points_earned, _completion_check, _numbered_task_coverage


def test_declared_incomplete_work_is_blocked():
    result = _completion_check("y' = 1", {"work_complete": False, "completion_evidence": "ends with minus"})
    assert result["complete"] is False
    assert result["source"] == "vision"


def test_terminal_operator_is_blocked():
    result = _completion_check("y' = (u'v - uv') / v^2 -", {"work_complete": True})
    assert result["complete"] is False
    assert result["source"] == "ocr"


def test_unclosed_delimiter_is_blocked():
    result = _completion_check(r"y' = \frac{x+1}{x-1", {"work_complete": True})
    assert result["complete"] is False


def test_complete_unsimplified_quotient_rule_is_allowed():
    work = r"y' = \frac{(1+\sec^2 x)(x-\tan x)-(x+\tan x)(1-\sec^2 x)}{(x-\tan x)^2}"
    result = _completion_check(work, {"work_complete": True})
    assert result["complete"] is True


def test_correct_verdict_cannot_override_independent_incomplete_audit():
    result = _completion_check("y' = 3x^2 + 3/(2√x) + 2/x^2", {"work_complete": False, "correct": True, "completion_evidence": "fraction unclosed"})
    assert result["complete"] is False
    assert result["source"] == "vision"


def test_all_rubric_points_earned_requires_each_step_to_be_full():
    assert _all_rubric_points_earned({"step_scores": [{"score": 1, "max_score": 1}, {"score": 2, "max_score": 2}]}) is True
    assert _all_rubric_points_earned({"step_scores": [{"score": 1, "max_score": 1}, {"score": 1, "max_score": 2}]}) is False


def test_numbered_task_coverage_detects_missing_second_part():
    problem = r"""(1) $(\cot x)'= -\csc^2 x$；
(2) $(\csc x)'= -\csc x\cot x$。"""
    result = _numbered_task_coverage(problem, "(cot x)' = -csc^2 x")
    assert result["available"] is True
    assert result["matched"] == 1
    assert result["required"] == 2
    assert result["missing"] == ["2"]


def test_numbered_task_coverage_accepts_all_parts():
    problem = r"""(1) $(\cot x)'= -\csc^2 x$；
(2) $(\csc x)'= -\csc x\cot x$。"""
    result = _numbered_task_coverage(problem, "(cot x)'=-csc^2 x; (csc x)'=-csc x cot x")
    assert result["matched"] == result["required"] == 2
