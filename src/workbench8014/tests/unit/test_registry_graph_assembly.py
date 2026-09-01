from pathlib import Path
import sys

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from math_agent_graph import build_math_agent
from skills.registry import registry
from skills.schemas import VerificationResult


def test_graph_uses_the_registry_bound_at_compile_time_and_records_variant_metadata():
    variant = registry.clone()

    def always_correct(_payload):
        return VerificationResult(
            success=True, correct=True, confidence=0.99,
            method="experiment-stub",
        )

    variant.replace(
        "symbolic_verification",
        always_correct,
        version="experiment-2.0.0",
        config={"experiment": "symbolic-ab", "arm": "b"},
    )
    agent = build_math_agent(
        skill_registry=variant,
        runtime_config={
            "graph_variant": "symbolic-ab-b",
            "model_version": "not-used-in-this-path",
        },
    )
    result = agent.invoke({
        "student_answer": "intentionally-not-an-expression",
        "standard_answer": "x",
        "problem_text": "测试题",
        "question_type": "calc",
        "mode": "diagnose",
        "execution_trace": [],
    })

    first = result["execution_trace"][0]
    assert result["verification"]["method"] == "experiment-stub"
    assert first["graph_variant"] == "symbolic-ab-b"
    assert first["routing_policy_version"] == "2026.08.step-aware-v2"
    assert first["skills"] == ["symbolic_verification"]
    assert first["skill_manifest"] == [{
        "name": "symbolic_verification",
        "version": "experiment-2.0.0",
        "config": {"experiment": "symbolic-ab", "arm": "b"},
    }]
