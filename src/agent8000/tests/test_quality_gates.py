import pytest

from app.quality_gates import audit_rubric, normalize_step_scores


def test_mismatched_generic_rubric_falls_back_to_standard_answer():
    problem = "(1) $(\\cot x)'$；\n(2) $(\\csc x)'$。"
    answer = "(1) $(\\cot x)'=-\\csc^2x$；\n(2) $(\\csc x)'=-\\csc x\\cot x$。"
    rubric = '[{"key":"derive","weight":2},{"key":"conclude","weight":2}]'
    result = audit_rubric(problem, answer, rubric, 10)
    assert result["valid"] is False
    assert result["source"] == "standard_answer_fallback"
    assert result["effective_solution"] == answer
    assert any("题目满分" in issue for issue in result["issues"])
    assert any("全部编号小问" in issue for issue in result["issues"])


def test_numbered_standard_answer_must_cover_every_part():
    result = audit_rubric("(1) $f'$\n(2) $g'$", "(1) $f'=1$", "按结果评分", 10)
    assert result["valid"] is False
    assert any("第 2 问" in issue for issue in result["issues"])


def test_step_scores_are_scaled_to_question_maximum():
    model = {"step_scores": [
        {"step": "a", "score": 7, "max_score": 7},
        {"step": "b", "score": 3, "max_score": 6},
        {"step": "c", "score": 5, "max_score": 5},
        {"step": "d", "score": 4, "max_score": 4},
    ]}
    audit = normalize_step_scores(model, 10)
    assert audit["normalized"] is True
    assert sum(step["max_score"] for step in model["step_scores"]) == pytest.approx(10, abs=0.002)
    assert all(step["score"] <= step["max_score"] for step in model["step_scores"])


def test_aligned_step_scores_remain_unchanged():
    model = {"step_scores": [{"step": "result", "score": 5, "max_score": 10}]}
    audit = normalize_step_scores(model, 10)
    assert audit["normalized"] is False
    assert model["step_scores"][0]["score"] == 5
