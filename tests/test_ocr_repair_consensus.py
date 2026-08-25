from src.workbench8014.ocr_repair_consensus import decide


def test_agreement_can_be_recommended_but_not_written() -> None:
    result = decide([
        {"provider": "mineru", "latex_text": r"\int_0^1 x^2 dx", "confidence": .9},
        {"provider": "vlm", "latex_text": r"\int_{0}^{1}x^2dx", "confidence": .8},
    ])
    assert result["decision"] == "AUTO_ACCEPT"


def test_critical_exponent_conflict_requires_teacher() -> None:
    result = decide([
        {"provider": "mineru", "latex_text": r"x^2", "confidence": .9},
        {"provider": "formula", "latex_text": r"x^3", "confidence": .9},
        {"provider": "vlm", "latex_text": r"x^2", "confidence": .9},
    ])
    assert result["decision"] == "NEEDS_TEACHER_REVIEW"
