from app.education_document_tools.assemble_assignment import _assignment_quality_reason


def test_assignment_selection_rejects_question_without_rubric():
    stem = (chr(0x6C42) + " x") * 6
    assert _assignment_quality_reason({
        "content": stem, "answer": "answer", "rubric": "",
    }) is not None
    assert _assignment_quality_reason({
        "content": stem, "answer": "answer", "rubric": "steps",
    }) is None
