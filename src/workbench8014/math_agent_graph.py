# -*- coding: utf-8 -*-
"""LangGraph 编排的高数学习 Agent。"""
from __future__ import annotations

import sys
import time
import uuid
import re
import importlib
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict
from functools import partial

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from langgraph.graph import END, START, StateGraph
from grading_engine import expr_equal
from skills.registry import SkillRegistry, registry as default_registry


class MathAgentState(TypedDict, total=False):
    problem_text: str
    section_no: str
    problem_no: str
    problem_id: str
    question_type: str
    student_answer: str
    student_steps: str
    standard_answer: str
    verification: dict
    independent_solution: dict
    solution_comparison: dict
    diagnosis: dict
    evidence: dict
    trace_id: str
    execution_trace: list[dict]
    mode: str
    teacher_feedback: str
    proof_assessment: dict
    action: Literal["diagnose_misconception", "independent_solve", "teacher_review"]
    response: str


SKILL_MODULES = {
    "symbolic_verification": "skills.symbolic_verification",
    "independent_solving": "skills.independent_solving",
    "misconception_diagnosis": "skills.misconception_diagnosis",
    "evidence_retrieval": "skills.evidence_retrieval",
}

ROUTING_POLICY_VERSION = "2026.08.step-aware-v2"
AGENT_RUNTIME_VERSION = "math-learning-agent-0.2.0"
GRAPH_SKILLS = (
    "symbolic_verification", "independent_solving", "misconception_diagnosis",
    "evidence_retrieval", "teaching_policy", "human_review",
)


def _load_builtin_skill(name: str) -> None:
    module = SKILL_MODULES.get(name)
    if module:
        importlib.import_module(module)


def resolve_skill(name: str, skill_registry: SkillRegistry | None = None):
    """Resolve an active skill from the graph-bound Registry."""
    _load_builtin_skill(name)
    return (skill_registry or default_registry).get(name)


def skill_metadata(name: str, skill_registry: SkillRegistry | None = None) -> dict:
    _load_builtin_skill(name)
    return (skill_registry or default_registry).describe(name)


def _runtime_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    values = {
        "agent_runtime_version": AGENT_RUNTIME_VERSION,
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "model_version": "local-qwen-configured",
        "graph_variant": "baseline",
    }
    values.update(dict(config or {}))
    return values


def _normalise_proof_text(text: str) -> str:
    """Normalise only layout noise, never mathematical signs, for proof evidence.

    The result is used as a conservative *presence* detector: a positive match
    prevents a false claim that a displayed key step is missing. It is not a
    replacement for teacher grading.
    """
    return re.sub(r"[\s$\\{}()\[\]，,。；;：:]", "", str(text or "")).lower()


def proof_step_assessment(student_answer: str, teacher_feedback: str = "", force_proof: bool = False, student_steps: str = "") -> dict:
    answer_text = str(student_answer or "").strip()
    steps_text = str(student_steps or "").strip()
    # Proof evidence may live in earlier handwritten step images, not only in
    # the final-image candidate answer. Presence checks remain conservative.
    raw = "\n".join(part for part in (answer_text, steps_text) if part)
    n = _normalise_proof_text(raw)
    h_key = "h-x=f-x-fx=-hx" in n or ("h-x" in n and "f-x-fx" in n and "-hx" in n)
    g_key = "g-x=f-x+fx=gx" in n or ("g-x" in n and "f-x+fx" in n and "=gx" in n)
    first_complete = g_key and h_key
    relevant = force_proof or "h(" in raw or "g(" in raw or "奇函数" in raw or "偶函数" in raw
    decomposition = ("fx=" in n and "gx" in n and "hx" in n and ("1/2" in raw or "12" in n or "frac12" in n))
    conclusion = "偶函数" in raw and "奇函数" in raw and ("表示" in raw or "和" in raw)
    unreadable = not raw or raw in {"未识别到作答", "未识别"} or len(n) < 8
    step_indices = re.findall(r"【步骤图\s*(\d+)】", steps_text)
    evidence_location = ("第 " + "、".join(dict.fromkeys(step_indices)) + " 张步骤图") if step_indices else ""
    if decomposition and conclusion:
        part2_status, part2_evidence = "confirmed", "已识别到分解公式及“偶函数与奇函数之和”的结论。"
    elif unreadable or not relevant:
        part2_status, part2_evidence = "uncertain", "作答文本不足或识别不清，无法判断第（2）问是否已完成。"
    else:
        part2_status, part2_evidence = "missing", "当前作答中未识别到第（2）问所需的分解公式与结论。"
    feedback = re.sub(r"\s+", "", str(teacher_feedback or ""))
    stale_part1_claim = any(x in feedback for x in ("没有正确地证明h", "未证明h", "h(x)是否奇函数", "未能正确"))
    negative_part2_claim = any(x in feedback for x in ("第（2）问错误", "第(2)问错误", "未完成第（2）问", "未完成第(2)问", "缺少分解公式"))
    feedback_conflict = bool((first_complete and stale_part1_claim) or (part2_status == "confirmed" and negative_part2_claim))
    return {
        "applicable": relevant,
        "subquestions": [
            {"label": "第（1）问", "status": "correct" if first_complete else "needs_teacher_confirmation", "evidence": ("已识别到 $g(-x)=g(x)$ 与 $h(-x)=f(-x)-f(x)=-h(x)$ 的关键证明。" if first_complete else "尚未可靠识别到两个奇偶性关键等式。") + (f" 证据来自{evidence_location}。" if evidence_location else "")},
            {"label": "第（2）问", "status": "correct" if part2_status == "confirmed" else ("missing" if part2_status == "missing" else "needs_teacher_confirmation"), "evidence": part2_evidence + (f" 证据来自{evidence_location}。" if evidence_location else "")},
        ],
        "part2_status": part2_status, "part2_evidence": part2_evidence,
        "key_evidence_present": h_key, "teacher_feedback_conflict": feedback_conflict,
        "evidence_location": evidence_location,
    }


def _observe_node(
    node_name: str,
    skills: list[str],
    fn: Callable[[MathAgentState], dict],
    skill_registry: SkillRegistry,
    runtime_config: dict[str, Any],
) -> Callable[[MathAgentState], dict]:
    """Attach a privacy-safe, versioned execution event to every graph node."""
    def wrapped(state: MathAgentState) -> dict:
        started = time.perf_counter()
        output = fn(state)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        candidates = (
            output.get("verification") or output.get("independent_solution")
            or output.get("diagnosis") or output.get("evidence") or {}
        )
        event = {
            "node": node_name,
            "skills": list(skills),
            "skill_manifest": skill_registry.manifest(skills),
            "skill_versions": {item["name"]: item["version"] for item in skill_registry.manifest(skills)},
            "skill_configs": {item["name"]: item["config"] for item in skill_registry.manifest(skills)},
            "routing_policy_version": runtime_config["routing_policy_version"],
            "agent_runtime_version": runtime_config["agent_runtime_version"],
            "graph_variant": runtime_config["graph_variant"],
            "model_version": candidates.get("model_name") or candidates.get("provider") or runtime_config["model_version"],
            "latency_ms": elapsed_ms,
            "success": bool(candidates.get("success", candidates.get("available", True))),
            "confidence": candidates.get("confidence"),
            "error_code": candidates.get("error_code"),
            "action": output.get("action"),
        }
        output["execution_trace"] = [*state.get("execution_trace", []), event]
        return output
    return wrapped


def verify_answer(state: MathAgentState, skill_registry: SkillRegistry | None = None) -> dict:
    """LangGraph 节点只负责路由；具体判等交给 SymPy Skill。"""
    from skills.schemas import SymbolicVerificationInput
    symbolic_verification = resolve_skill("symbolic_verification", skill_registry)

    question_type = state.get("question_type", "calc")
    if question_type == "proof":
        proof = proof_step_assessment(state.get("student_answer", ""), state.get("teacher_feedback", ""), force_proof=True, student_steps=state.get("student_steps", ""))
        verification = {"correct": None, "confidence": 0.0, "method": "证明题关键步骤核对"}
        first_uncertain = proof["subquestions"][0]["status"] != "correct"
        action = "teacher_review" if (first_uncertain or proof["part2_status"] == "uncertain" or proof["teacher_feedback_conflict"]) else "diagnose_misconception"
        return {"verification": verification, "proof_assessment": proof, "action": action}
    result = symbolic_verification(SymbolicVerificationInput(
        student_answer=state["student_answer"],
        standard_answer=state["standard_answer"],
    ))
    verification = result.model_dump(exclude_none=True)
    proof = proof_step_assessment(state.get("student_answer", ""), state.get("teacher_feedback", ""), student_steps=state.get("student_steps", ""))
    action = "diagnose_misconception" if (result.confidence or 0) >= 0.85 else "independent_solve"
    return {"verification": verification, "proof_assessment": proof, "action": action}


def independent_solve(state: MathAgentState, skill_registry: SkillRegistry | None = None) -> dict:
    """LangGraph 节点调用 Qwen 独立求解 Skill；不传入标准答案。"""
    independent_solving = resolve_skill("independent_solving", skill_registry)
    from skills.schemas import IndependentSolveInput

    problem_text = state.get("problem_text", "").strip()
    if not problem_text:
        return {"independent_solution": {
            "available": False, "reason": "题目正文缺失，不能独立求解",
            "error_code": "MISSING_PROBLEM_TEXT",
        }}
    result = independent_solving(IndependentSolveInput(
        problem_text=problem_text,
        section_no=state.get("section_no", ""),
        problem_no=state.get("problem_no", ""),
    ))
    solution = result.model_dump(exclude_none=True)
    solution["available"] = result.success
    if not result.success:
        solution["reason"] = (result.warnings or ["独立求解服务不可用"])[0]
    return {"independent_solution": solution}


def compare_solutions(state: MathAgentState, skill_registry: SkillRegistry | None = None) -> dict:
    solution = state.get("independent_solution", {})
    if not solution.get("available"):
        return {"solution_comparison": {"consistent": False, "reason": solution.get("reason", "独立求解失败")}, "action": "teacher_review"}
    from skills.schemas import SymbolicVerificationInput
    symbolic_verification = resolve_skill("symbolic_verification", skill_registry)

    solved_answer = str(solution.get("answer", "")).rsplit("=", 1)[-1].strip()
    verdict = symbolic_verification(SymbolicVerificationInput(
        student_answer=solved_answer, standard_answer=state["standard_answer"]
    ))
    model_confidence = float(solution.get("confidence") or 0)
    consistent = bool(verdict.correct) and (verdict.confidence or 0) >= 0.85 and model_confidence >= 0.70
    return {
        "solution_comparison": {
            "consistent": consistent, "confidence": verdict.confidence,
            "method": verdict.method, "solver_confidence": model_confidence,
        },
        "action": "diagnose_misconception" if consistent else "teacher_review",
    }


def diagnose_misconception(state: MathAgentState, skill_registry: SkillRegistry | None = None) -> dict:
    """用结构化错因 Skill 为教学策略提供可解释依据。"""
    misconception_diagnosis = resolve_skill("misconception_diagnosis", skill_registry)
    from skills.schemas import MisconceptionDiagnosisInput

    # Qwen 已独立确认一致时，原始 OCR/文本格式不可靠，不应再把它误诊为数学错误。
    if state.get("solution_comparison", {}).get("consistent"):
        return {"diagnosis": {
            "success": True, "confidence": 0.90, "diagnoses": [],
            "summary": "独立求解已确认数学结果正确，不进行错误标签诊断。",
            "evidence": ["Qwen 独立求解与标准答案通过符号判等。"],
        }, "action": "teach_student"}

    result = misconception_diagnosis(MisconceptionDiagnosisInput(
        student_answer=state["student_answer"],
        standard_answer=state["standard_answer"],
        problem_text=state.get("problem_text", ""),
        verification_correct=state.get("verification", {}).get("correct"),
        intermediate_steps=state.get("student_steps", ""),
    ))
    evidence = {"success": False, "warnings": ["未关联题库原题，无法提供教材证据。"]}
    if state.get("problem_id"):
        evidence_retrieval = resolve_skill("evidence_retrieval", skill_registry)
        from skills.schemas import EvidenceRetrievalInput
        evidence = evidence_retrieval(EvidenceRetrievalInput(problem_id=state["problem_id"])).model_dump(exclude_none=True)
    return {"diagnosis": result.model_dump(exclude_none=True), "evidence": evidence, "action": "teach_student"}


@default_registry.register("teaching_policy", version="1.0.0", config={"style": "post_submission_scaffolded"})
def teach_student(state: MathAgentState) -> dict:
    proof = state.get("proof_assessment", {})
    mode = state.get("mode", "diagnose")
    if proof.get("applicable"):
        part2_status = proof.get("part2_status")
        if part2_status == "confirmed":
            base = "第（1）问与第（2）问的关键证据均已识别：奇偶性证明、分解公式和结论完整。"
            hint = "请再检查分解式中的 $\\frac12$ 系数与最终表述是否保持一致。"
        else:
            base = "第（1）问已识别为正确。第（2）问当前作答中未识别到分解公式与结论。"
            hint = "补写 $f(x)=\\frac12[f(x)+f(-x)]+\\frac12[f(x)-f(-x)]$，并说明前一项为偶函数、后一项为奇函数。"
        if mode == "hint":
            text = "下一步提示：" + hint
        elif mode == "solution":
            text = base + chr(10) * 2 + "完整推导：" + chr(10) + "令 $g(x)=f(x)+f(-x)$，则 $g(-x)=g(x)$，故 $g$ 为偶函数；令 $h(x)=f(x)-f(-x)$，则 $h(-x)=-h(x)$，故 $h$ 为奇函数。又 $f(x)=\\frac12g(x)+\\frac12h(x)$，所以 $f$ 可表示为一个偶函数与一个奇函数之和。"
        else:
            text = base + chr(10) * 2 + "错因分析：" + proof.get("part2_evidence", hint)
        return {"response": text}
    verification = state["verification"]
    solution = state.get("independent_solution", {})
    diagnosis = state.get("diagnosis", {})
    evidence = state.get("evidence", {})
    items = diagnosis.get("diagnoses") or []
    mode = state.get("mode", "diagnose")
    if mode not in {"diagnose", "hint", "solution"}:
        mode = "diagnose"
    if verification["correct"]:
        base = "你的答案正确：{}（置信度 {:.2f}）。".format(verification["method"], verification["confidence"])
    elif state.get("solution_comparison", {}).get("consistent"):
        base = "表达式格式未识别，但数学结果正确：独立求解与标准答案已通过符号判等。"
    else:
        base = "答案暂不正确：{}（置信度 {:.2f}）。".format(verification["method"], verification["confidence"])

    if mode == "hint":
        next_step = (items[0]["label"] + "：" + items[0]["next_step"]) if items else "请先写出关键中间步骤，再逐项核对符号、幂次和条件。"
        text = base + chr(10) * 2 + "下一步提示：" + next_step
    elif mode == "solution" and solution.get("full_solution"):
        text = base + chr(10) * 2 + "完整推导（用于提交后的复盘）：" + chr(10) + str(solution["full_solution"])
    elif mode == "solution":
        text = base + chr(10) * 2 + "暂时没有可靠的完整推导。为避免误导，建议教师复核后补充。"
    elif items:
        source_label = {"student_steps": "你补充的步骤", "final_answer": "最终答案", "problem_requirement": "题目条件"}
        details = chr(10).join(
            f"- {item['label']}（依据：{source_label.get(item.get('evidence_source'), '当前作答')}{'；定位：' + item.get('evidence_location') if item.get('evidence_location') else ''}）：{item['evidence']} 建议：{item['next_step']}" for item in items
        )
        text = base + chr(10) * 2 + "结构化错因诊断：" + chr(10) + details
    else:
        text = base + chr(10) * 2 + "错因诊断：" + diagnosis.get(
            "summary", "最终结果不一致，但现有信息不足以可靠定位原因；请补充关键中间步骤。"
        )
    record = evidence.get("record") or {}
    if mode == "diagnose" and record:
        source = f"复盘依据：教材第 {record.get('section_no', '')} 节，第 {record.get('problem_no', '')} 题"
        points = record.get("knowledge_points") or []
        if points:
            source += "；知识点：" + "、".join(points)
        text += chr(10) * 2 + source
    elif mode == "diagnose" and evidence.get("warnings"):
        text += chr(10) * 2 + "证据状态：" + str(evidence["warnings"][0])
    return {"response": text}


@default_registry.register("human_review", version="1.0.0", config={"fallback": "teacher_confirmation"})
def teacher_review(state: MathAgentState) -> dict:
    mode = state.get("mode", "diagnose")
    proof = state.get("proof_assessment", {})
    if proof.get("applicable"):
        reason = "当前识别到的关键步骤与既有评分反馈存在冲突" if proof.get("teacher_feedback_conflict") else "手写识别或证明关键步骤不足，不能可靠确认"
        if mode == "hint":
            learning = "下一步提示：请重新上传包含完整第（1）、第（2）问的清晰图片，尤其要拍到分解公式和结论。"
        elif mode == "solution":
            learning = "参考推导：令 $g(x)=f(x)+f(-x)$，$h(x)=f(x)-f(-x)$。可得 $g(-x)=g(x)$、$h(-x)=-h(x)$，且 $f(x)=\\frac12g(x)+\\frac12h(x)$。"
        else:
            learning = "当前无法可靠确认第（2）问的证据：" + proof.get("part2_evidence", "请补充清晰作答。")
        return {"response": learning + chr(10) * 2 + "需要教师确认：" + reason + "。", "proof_assessment": proof}
    reason = state.get("solution_comparison", {}).get("reason") or "独立求解与标准答案未能可靠一致"
    return {"response": f"该题需要教师复核：{reason}。系统不会在低置信度时编造讲解。"}


def choose_next(state: MathAgentState) -> str:
    return state["action"]


def build_math_agent(
    skill_registry: SkillRegistry | None = None,
    runtime_config: dict[str, Any] | None = None,
):
    """Compile a graph bound to one Registry snapshot.

    Pass a cloned Registry plus replace(...) to test a Skill variant without
    altering the production graph.
    """
    active_registry = skill_registry or default_registry
    for name in SKILL_MODULES:
        _load_builtin_skill(name)
    missing = [name for name in GRAPH_SKILLS if name not in active_registry.names()]
    if missing:
        raise ValueError("图缺少必需 Skill：" + ", ".join(missing))
    config = _runtime_config(runtime_config)
    graph = StateGraph(MathAgentState)
    graph.add_node("verify_answer", _observe_node("verify_answer", ["symbolic_verification"], partial(verify_answer, skill_registry=active_registry), active_registry, config))
    graph.add_node("independent_solve", _observe_node("independent_solve", ["independent_solving"], partial(independent_solve, skill_registry=active_registry), active_registry, config))
    graph.add_node("compare_solutions", _observe_node("compare_solutions", ["symbolic_verification"], partial(compare_solutions, skill_registry=active_registry), active_registry, config))
    graph.add_node("diagnose_misconception", _observe_node("diagnose_misconception", ["misconception_diagnosis", "evidence_retrieval"], partial(diagnose_misconception, skill_registry=active_registry), active_registry, config))
    graph.add_node("teach_student", _observe_node("teach_student", ["teaching_policy"], active_registry.get("teaching_policy"), active_registry, config))
    graph.add_node("teacher_review", _observe_node("teacher_review", ["human_review"], active_registry.get("human_review"), active_registry, config))
    graph.add_edge(START, "verify_answer")
    graph.add_conditional_edges("verify_answer", choose_next, {"diagnose_misconception": "diagnose_misconception", "independent_solve": "independent_solve", "teacher_review": "teacher_review"})
    graph.add_edge("independent_solve", "compare_solutions")
    graph.add_conditional_edges("compare_solutions", choose_next, {"diagnose_misconception": "diagnose_misconception", "teacher_review": "teacher_review"})
    graph.add_edge("diagnose_misconception", "teach_student")
    graph.add_edge("teach_student", END)
    graph.add_edge("teacher_review", END)
    return graph.compile()



math_agent = build_math_agent()


def run_math_agent(student_answer: str, standard_answer: str, problem_text: str = "", section_no: str = "", problem_no: str = "", mode: str = "diagnose", problem_id: str = "", teacher_feedback: str = "", question_type: str = "calc", student_steps: str = "") -> dict:
    return math_agent.invoke({
        "student_answer": student_answer, "student_steps": student_steps, "standard_answer": standard_answer,
        "problem_text": problem_text, "section_no": section_no, "problem_no": problem_no,
        "mode": mode, "problem_id": problem_id, "teacher_feedback": teacher_feedback, "question_type": question_type, "trace_id": str(uuid.uuid4()),
        "execution_trace": [],
    })
