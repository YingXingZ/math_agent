from pathlib import Path
import sys

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from skills.misconception_diagnosis import misconception_diagnosis
from skills.schemas import MisconceptionDiagnosisInput


def diagnose(student: str, standard: str, problem: str = ""):
    return misconception_diagnosis(MisconceptionDiagnosisInput(
        student_answer=student, standard_answer=standard, problem_text=problem
    ))


def test_sign_error_has_math_evidence():
    result = diagnose("-x^2", "x^2")
    assert result.success is True
    assert result.diagnoses[0].code == "SIGN_ERROR"
    assert "相反数" in result.diagnoses[0].evidence


def test_exponent_error_is_labeled():
    result = diagnose("x^2", "x^3")
    assert any(item.code == "EXPONENT_ERROR" for item in result.diagnoses)


def test_correct_answer_has_no_misconception():
    result = misconception_diagnosis(MisconceptionDiagnosisInput(
        student_answer="x", standard_answer="x", verification_correct=True
    ))
    assert result.diagnoses == []
    assert "正确" in result.summary


def test_unknown_error_does_not_invent_label():
    result = diagnose("x+1", "x+2")
    assert result.error_code == "NEED_INTERMEDIATE_STEPS"
    assert result.diagnoses == []


def test_chain_rule_omission_is_reported_when_inner_factor_is_missing():
    from skills.schemas import MisconceptionDiagnosisInput
    from skills.misconception_diagnosis import misconception_diagnosis
    result = misconception_diagnosis(MisconceptionDiagnosisInput(
        student_answer="cos(x^2)", standard_answer="2*x*cos(x^2)",
        problem_text="求导 y=sin(x^2)", verification_correct=False, intermediate_steps="先对 sin 求导"))
    assert "CHAIN_RULE_OMITTED" in {item.code for item in result.diagnoses}
