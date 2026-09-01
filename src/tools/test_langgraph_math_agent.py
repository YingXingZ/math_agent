# -*- coding: utf-8 -*-
"""LangGraph 高数答案校验 Agent 的行为测试。"""
from pathlib import Path
import sys

WORKBENCH_DIR = Path(__file__).resolve().parents[1] / "workbench8014"
sys.path.insert(0, str(WORKBENCH_DIR))

from math_agent_graph import run_math_agent


def test_correct_answer_returns_student_feedback():
    result = run_math_agent("(x+1)^2", "x^2+2*x+1")
    assert result["verification"]["correct"] is True
    assert result["action"] == "teach_student"
    assert "答案正确" in result["response"]


def test_wrong_answer_returns_student_feedback():
    result = run_math_agent("x^2", "x^3")
    assert result["verification"]["correct"] is False
    assert result["action"] == "teach_student"
    assert "答案暂不正确" in result["response"]


def test_ocr_superscript_is_normalized_before_agent_routing():
    result = run_math_agent("x²+2x+1", "(x+1)^2")
    assert result["verification"]["correct"] is True
    assert result["action"] == "teach_student"
    assert "答案正确" in result["response"]


def test_agent_returns_structured_sign_diagnosis():
    result = run_math_agent("-x^2", "x^2", mode="diagnose")
    assert result["diagnosis"]["diagnoses"][0]["code"] == "SIGN_ERROR"
    assert "符号错误" in result["response"]


def test_agent_hint_uses_diagnosis_next_step():
    result = run_math_agent("x^2", "x^3", mode="hint")
    assert result["diagnosis"]["diagnoses"][0]["code"] == "EXPONENT_ERROR"
    assert "幂次" in result["response"]
    assert "完整推导" not in result["response"]


def test_agent_execution_trace_contains_skill_versions_and_timing():
    result = run_math_agent("x^2", "x^3", mode="diagnose")
    assert result["trace_id"]
    assert [event["node"] for event in result["execution_trace"]] == [
        "verify_answer", "diagnose_misconception", "teach_student"
    ]
    assert result["execution_trace"][0]["skills"] == ["symbolic_verification"]
    assert "symbolic_verification" in result["execution_trace"][0]["skill_versions"]
    assert isinstance(result["execution_trace"][0]["latency_ms"], float)
