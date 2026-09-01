from pathlib import Path
import sys

import pytest

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from skills.registry import SkillRegistry
from skills.schemas import IndependentSolveInput, VerificationResult


def test_verification_result_has_stable_defaults():
    result = VerificationResult(success=True, correct=True, confidence=0.99, method="sympy")
    assert result.evidence == []
    assert result.warnings == []
    assert result.error_code is None


def test_independent_solve_input_rejects_empty_problem():
    with pytest.raises(Exception):
        IndependentSolveInput(problem_text="")


def test_registry_registers_and_resolves_skill():
    local = SkillRegistry()

    @local.register("demo_skill")
    def demo(value: int) -> int:
        return value + 1

    assert local.names() == ("demo_skill",)
    assert local.get("demo_skill")(2) == 3


def test_registry_rejects_duplicate_names():
    local = SkillRegistry()
    local.register("demo_skill")(lambda: None)
    with pytest.raises(ValueError):
        local.register("demo_skill")(lambda: None)


def test_registry_can_replace_an_active_skill_for_an_experiment():
    local = SkillRegistry()
    local.register("demo_skill", version="1.0.0")(lambda value: value + 1)
    local.replace("demo_skill", lambda value: value + 2, version="2.0.0", config={"variant": "b"})
    assert local.get("demo_skill")(2) == 4
    assert local.describe("demo_skill") == {"name": "demo_skill", "version": "2.0.0", "config": {"variant": "b"}}
