from src.agent8000.app.assignment_pdf import (
    POINTS_PER_QUESTION, display_problem_no, strip_source_problem_prefix,
)


def test_assignment_uses_sequential_number_not_source_number() -> None:
    assert display_problem_no({"sort_order": 3, "original_no": "12"}, 2) == "3"
    assert POINTS_PER_QUESTION == 10


def test_assignment_strips_any_source_problem_prefix() -> None:
    assert strip_source_problem_prefix("12. 已知正六边形") == "已知正六边形"
    assert strip_source_problem_prefix("7、求极限") == "求极限"
