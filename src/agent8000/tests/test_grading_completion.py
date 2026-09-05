from app.grading_pipeline import _completion_check


def test_declared_incomplete_work_is_blocked():
    result = _completion_check("y' = 1", {"work_complete": False, "completion_evidence": "ends with minus"})
    assert result["complete"] is False
    assert result["source"] == "vision"


def test_terminal_operator_is_blocked():
    result = _completion_check("y' = (u'v - uv') / v^2 -", {"work_complete": True})
    assert result["complete"] is False
    assert result["source"] == "ocr"


def test_unclosed_delimiter_is_blocked():
    result = _completion_check(r"y' = rac{x+1}{x-1", {"work_complete": True})
    assert result["complete"] is False


def test_complete_unsimplified_quotient_rule_is_allowed():
    work = r"y' = rac{(1+\sec^2 x)(x-	an x)-(x+	an x)(1-\sec^2 x)}{(x-	an x)^2}"
    result = _completion_check(work, {"work_complete": True})
    assert result["complete"] is True


def test_correct_verdict_does_not_fail_on_false_completion_flag():
    result = _completion_check("y' = 3x^2 + 3/(2√x) + 2/x^2", {"work_complete": False, "correct": True, "completion_evidence": "fraction unclosed"})
    assert result["complete"] is True
