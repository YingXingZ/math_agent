from pathlib import Path
import sys

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from skills.schemas import SymbolicVerificationInput
from skills.symbolic_verification import symbolic_verification


def test_equivalent_forms_are_verified_by_skill():
    result = symbolic_verification(SymbolicVerificationInput(student_answer="(x+1)^2", standard_answer="x^2+2*x+1"))
    assert result.success is True
    assert result.correct is True
    assert result.confidence == 0.99
    assert result.method == "符号化简判等"


def test_skill_does_not_return_standard_answer():
    result = symbolic_verification(SymbolicVerificationInput(student_answer="x^2", standard_answer="x^3"))
    dumped = result.model_dump()
    assert "x^3" not in str(dumped.values())


def test_non_mathematical_text_is_low_confidence():
    result = symbolic_verification(SymbolicVerificationInput(student_answer="答案见图", standard_answer="x^2"))
    assert result.correct is None
    assert result.confidence == 0.30
    assert result.error_code == "NON_MATHEMATICAL_ANSWER"
