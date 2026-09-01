"""Safe, traceable Tool Use for the mathematics Agent.

Tools are policy-routed by the LangGraph state, never selected from untrusted
student/OCR text.  Each call records a privacy-preserving input digest, latency,
version and whether its output influenced the final decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import Annotated, Any, Callable, TypedDict
import operator

from langgraph.graph import END, START, StateGraph


TOOL_ROUTER_VERSION = "tool-router-v2"
MAX_EXPRESSION_CHARS = 2400
MAX_QUERY_CHARS = 8000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    handler: Callable[..., dict[str, Any]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, *, version: str, description: str):
        def decorate(handler: Callable[..., dict[str, Any]]):
            self._tools[name] = ToolSpec(name, version, description, handler)
            return handler
        return decorate

    def names(self) -> list[str]:
        return sorted(self._tools)

    def manifest(self) -> list[dict[str, str]]:
        return [
            {"name": spec.name, "version": spec.version, "description": spec.description}
            for spec in sorted(self._tools.values(), key=lambda item: item.name)
        ]

    def invoke(self, name: str, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        spec = self._tools.get(name)
        if spec is None:
            raise ValueError(f"tool is not allowlisted: {name}")
        started = perf_counter()
        try:
            output = spec.handler(**kwargs)
            ok, error = True, ""
        except Exception as exc:  # Tool failures become evidence, never silent success.
            output, ok, error = {"available": False, "detail": "工具暂不可用"}, False, str(exc)[:180]
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        # Never persist full student/OCR text as an additional tool trace.
        safe_input = repr(sorted((key, str(value)[:MAX_QUERY_CHARS]) for key, value in kwargs.items()))
        trace = {
            "tool_name": spec.name,
            "tool_version": spec.version,
            "router_version": TOOL_ROUTER_VERSION,
            "ok": ok,
            "latency_ms": elapsed_ms,
            "input_sha256": sha256(safe_input.encode("utf-8")).hexdigest()[:16],
            "input_fields": sorted(kwargs),
            "output_fields": sorted(output) if isinstance(output, dict) else [],
            "error": error,
            "accepted": False,
        }
        return output, trace


registry = ToolRegistry()


_FORMULAS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("导数", "微分"), "导数法则", r"(uv)'=u'v+uv'；(f(g(x)))'=f'(g(x))g'(x)"),
    (("积分", "原函数"), "积分基本公式", r"\int f(x)\,dx=F(x)+C"),
    (("定积分", "面积"), "定积分性质", r"\int_a^b f(x)\,dx=F(b)-F(a)"),
    (("极限",), "极限运算法则", r"\lim(f\pm g)=\lim f\pm\lim g（需满足相应存在条件）"),
    (("泰勒", "近似"), "一阶泰勒公式", r"f(x)\approx f(x_0)+f'(x_0)(x-x_0)"),
    (("极值", "单调"), "闭区间最值检查", r"比较驻点、不可导点与区间端点的函数值"),
)


@registry.register("answer_evidence_lookup", version="1.0.0", description="只读查询已发布题库的答案、评分点与来源；仅接受内部题目 ID")
def answer_evidence_lookup(*, question_id: int) -> dict[str, Any]:
    # question_id comes from the assignment database row, never from student/OCR
    # text.  This keeps question-bank access inside the deterministic trust boundary.
    try:
        safe_id = int(question_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("question_id must be an integer") from exc
    if safe_id <= 0:
        raise ValueError("question_id must be positive")
    from .db import connection
    with connection() as conn:
        row = conn.execute(
            """SELECT id,answer,rubric,question_type,source_problem_id,review_status
                 FROM questions WHERE id=?""",
            (safe_id,),
        ).fetchone()
    if not row or str(row["review_status"] or "") != "published":
        return {"available": False, "detail": "没有可用于自动判定的已发布题库证据"}
    return {
        "available": bool(str(row["answer"] or "").strip()),
        "standard_answer": str(row["answer"] or ""),
        "rubric": str(row["rubric"] or ""),
        "question_type": str(row["question_type"] or ""),
        "source_problem_id": str(row["source_problem_id"] or ""),
        "source": "verified_question_bank",
    }


@registry.register("formula_lookup", version="1.0.0", description="只读教材公式参考检索；不返回题目答案")
def formula_lookup(*, query: str) -> dict[str, Any]:
    text = str(query or "")
    if len(text) > MAX_QUERY_CHARS:
        raise ValueError("formula query exceeds limit")
    matches = [
        {"topic": topic, "formula": formula}
        for keywords, topic, formula in _FORMULAS
        if any(keyword in text for keyword in keywords)
    ][:3]
    return {"available": bool(matches), "references": matches, "source": "curated_formula_catalog_v1"}


@registry.register("calculator", version="1.0.0", description="SymPy 独立表达式等价判定；不调用模型")
def calculator(*, student_expression: str, standard_expression: str) -> dict[str, Any]:
    student, standard = str(student_expression or "").strip(), str(standard_expression or "").strip()
    if len(student) > MAX_EXPRESSION_CHARS or len(standard) > MAX_EXPRESSION_CHARS:
        raise ValueError("expression exceeds limit")
    if not student or not standard:
        return {"available": False, "equal": None, "confidence": 0, "method": "缺少可判等表达式"}
    from pathlib import Path
    import sys
    project_root = str(Path(__file__).resolve().parents[2])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from grading_engine import expr_equal
    equal, confidence, method = expr_equal(student, standard)
    if not equal:
        raw = student.replace("＝", "=")
        if raw.count("=") == 1 and not any(token in raw for token in ("<", ">", "≤", "≥", "≠", "∈", "∉")):
            left, rhs = raw.split("=", 1)
            if left.strip() and rhs.strip():
                rhs_equal, rhs_confidence, rhs_method = expr_equal(rhs.strip(), standard)
                if rhs_equal:
                    return {
                        "available": True, "equal": True, "confidence": rhs_confidence,
                        "method": f"等式右端与标准答案一致（{rhs_method}）",
                    }
    return {"available": True, "equal": bool(equal), "confidence": float(confidence), "method": str(method)}


class ToolUseState(TypedDict, total=False):
    question_id: int
    question_type: str
    problem_text: str
    recognized_work: str
    standard_answer: str
    answer_evidence: dict[str, Any]
    formula_reference: dict[str, Any]
    math_equivalence: dict[str, Any]
    tool_trace: Annotated[list[dict[str, Any]], operator.add]


def _evidence_node(state: ToolUseState) -> dict[str, Any]:
    result, trace = registry.invoke("answer_evidence_lookup", question_id=state.get("question_id", 0))
    trace["accepted"] = bool(result.get("available"))
    return {"answer_evidence": result, "tool_trace": [trace]}


def _formula_node(state: ToolUseState) -> dict[str, Any]:
    result, trace = registry.invoke("formula_lookup", query=state.get("problem_text", ""))
    return {"formula_reference": result, "tool_trace": [trace]}


def _calculator_node(state: ToolUseState) -> dict[str, Any]:
    evidence = state.get("answer_evidence") or {}
    result, trace = registry.invoke(
        "calculator",
        student_expression=state.get("recognized_work", ""),
        standard_expression=evidence.get("standard_answer") or state.get("standard_answer", ""),
    )
    trace["accepted"] = bool(result.get("available"))
    return {"math_equivalence": result, "tool_trace": [trace]}


def _after_formula(state: ToolUseState) -> str:
    if state.get("question_type") == "calc" and state.get("recognized_work", "").strip() and state.get("standard_answer", "").strip():
        return "calculator"
    return "finish"


def _build_tool_graph():
    graph = StateGraph(ToolUseState)
    graph.add_node("answer_evidence_lookup", _evidence_node)
    graph.add_node("formula_lookup", _formula_node)
    graph.add_node("calculator", _calculator_node)
    graph.add_edge(START, "answer_evidence_lookup")
    graph.add_edge("answer_evidence_lookup", "formula_lookup")
    graph.add_conditional_edges("formula_lookup", _after_formula, {"calculator": "calculator", "finish": END})
    graph.add_edge("calculator", END)
    return graph.compile()


tool_use_graph = _build_tool_graph()


def run_tool_use(*, question_id: int, question_type: str, problem_text: str, recognized_work: str, standard_answer: str) -> dict[str, Any]:
    """Run the explicit LangGraph Tool Use subgraph for one grading item."""
    state = tool_use_graph.invoke({
        "question_id": int(question_id or 0),
        "question_type": str(question_type or ""),
        "problem_text": str(problem_text or ""),
        "recognized_work": str(recognized_work or ""),
        "standard_answer": str(standard_answer or ""),
        "tool_trace": [],
    })
    equivalence = state.get("math_equivalence") or {
        "available": False, "equal": None, "confidence": 0,
        "method": "未触发计算工具（证明题、空答案或空识别文本）",
    }
    traces = list(state.get("tool_trace") or [])
    # Mark formula context accepted only when the router retrieved a matching
    # safe reference. It never influences correct/incorrect scoring.
    for trace in traces:
        if trace["tool_name"] == "formula_lookup":
            trace["accepted"] = bool((state.get("formula_reference") or {}).get("available"))
    return {
        "math_equivalence": equivalence,
        "answer_evidence": state.get("answer_evidence") or {"available": False},
        "formula_reference": state.get("formula_reference") or {"available": False, "references": []},
        "tool_trace": traces,
        "tool_router_version": TOOL_ROUTER_VERSION,
    }
