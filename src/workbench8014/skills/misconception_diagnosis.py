"""基于确定性数学证据的错因诊断 Skill。

第一版只输出可验证的标签；无法可靠判断时要求学生提供步骤，而不是猜测。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from grading_engine import expr_equal, normalize_expr

from .registry import registry
from .schemas import DiagnosisItem, MisconceptionDiagnosisInput, MisconceptionDiagnosisResult

_FUNCTIONS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "exp", "sqrt"}
_DOMAIN_MARKERS = ("定义域", "x∈", r"x\in", "x in", "x>", "x<", "x≥", "x≤")


def _powers(expr: str) -> list[str]:
    return re.findall(r"(?:\^|\*\*)\(?(-?\d+)\)?", expr)


def _functions(expr: str) -> set[str]:
    return {name for name in _FUNCTIONS if re.search(rf"\b{name}\s*\(", expr)}


def _step_blocks(steps: str) -> list[tuple[int, str]]:
    """Return ordered blocks created by the multi-image step upload endpoint."""
    blocks = []
    pattern = re.compile(r"【步骤图\s*(\d+)】\s*(.*?)(?=【步骤图\s*\d+】|\Z)", re.S)
    for match in pattern.finditer(steps or ""):
        blocks.append((int(match.group(1)), match.group(2)))
    return blocks


def _step_location(steps: str, keywords: tuple[str, ...] = ()) -> str:
    blocks = _step_blocks(steps)
    if not blocks:
        return "学生补充的中间步骤"
    matching = [index for index, text in blocks if not keywords or any(word in text for word in keywords)]
    indices = matching or [index for index, _ in blocks]
    return "第 " + "、".join(str(index) for index in indices) + " 张步骤图"


@registry.register("misconception_diagnosis", version="1.1.0", config={"strategy": "step_aware_rules"})
def misconception_diagnosis(payload: MisconceptionDiagnosisInput) -> MisconceptionDiagnosisResult:
    """诊断最终答案中可由规则和符号验证支撑的典型错误。"""
    if payload.verification_correct is True:
        return MisconceptionDiagnosisResult(
            success=True, confidence=0.99, diagnoses=[],
            summary="答案已确认正确，无需错因诊断。",
            evidence=["SymPy 已确认答案与标准答案等价。"],
        )

    student = normalize_expr(payload.student_answer)
    standard = normalize_expr(payload.standard_answer)
    steps = str(payload.intermediate_steps or "")
    step_text = (payload.student_answer + "\n" + steps).replace(" ", "")
    if not student or not standard:
        return MisconceptionDiagnosisResult(
            success=False, confidence=0.0, diagnoses=[],
            summary="答案文本无法解析，不能可靠诊断错因。",
            warnings=["请补充清晰的手写步骤，或由教师复核。"],
            error_code="UNPARSEABLE_ANSWER",
        )

    diagnoses: list[DiagnosisItem] = []
    opposite, opposite_confidence, _ = expr_equal(payload.student_answer, f"-({payload.standard_answer})")
    if opposite and opposite_confidence >= 0.85:
        diagnoses.append(DiagnosisItem(
            code="SIGN_ERROR", label="符号错误", confidence=0.95,
            evidence="学生结果与标准结果互为相反数。",
            next_step="回看最后一次移项、展开括号或代入时，每一项的正负号。",
            evidence_source="final_answer",
        ))

    student_powers, standard_powers = _powers(student), _powers(standard)
    if student_powers and standard_powers and student_powers != standard_powers:
        diagnoses.append(DiagnosisItem(
            code="EXPONENT_ERROR", label="幂次错误", confidence=0.78,
            evidence=f"学生答案中的幂次 {student_powers} 与标准表达式中的幂次 {standard_powers} 不一致。",
            next_step="逐项检查乘方、求导或积分后指数应如何变化。",
            evidence_source="final_answer",
        ))

    student_functions, standard_functions = _functions(student), _functions(standard)
    if student_functions != standard_functions:
        diagnoses.append(DiagnosisItem(
            code="FORMULA_OR_METHOD_ERROR", label="公式或方法使用不当", confidence=0.72,
            evidence="学生答案与标准表达式使用的函数类型不一致。",
            next_step="回到题目条件，确认应使用的基本公式、求导法则或恒等变形。",
            evidence_source="final_answer",
        ))

    needs_domain = any(marker in payload.problem_text.replace(" ", "") for marker in _DOMAIN_MARKERS)
    student_has_domain = any(marker in payload.student_answer.replace(" ", "") for marker in _DOMAIN_MARKERS)
    standard_has_domain = any(marker in payload.standard_answer.replace(" ", "") for marker in _DOMAIN_MARKERS)
    if needs_domain and standard_has_domain and not student_has_domain:
        diagnoses.append(DiagnosisItem(
            code="DOMAIN_CONDITION_OMITTED", label="定义域或条件遗漏", confidence=0.75,
            evidence="题目要求或标准答案包含定义域/适用条件，但学生答案未包含对应条件。",
            next_step="检查题目给出的取值范围、分母不为零条件、对数真数为正等限制。",
            evidence_source="problem_requirement",
        ))

    problem = payload.problem_text.replace(" ", "")
    if ("积分" in problem or "∫" in problem) and re.search(r"(^|[^A-Za-z])C([^A-Za-z]|$)", payload.standard_answer) and not re.search(r"(^|[^A-Za-z])C([^A-Za-z]|$)", step_text):
        diagnoses.append(DiagnosisItem(
            code="INTEGRATION_CONSTANT_OMITTED", label="积分常数遗漏", confidence=0.88,
            evidence="标准答案含积分常数 C，但最终答案和补充步骤中均未出现 C。",
            next_step="不定积分完成后补写“+ C”，再检查常数是否已并入其他常数项。",
            evidence_source="student_steps" if steps else "final_answer",
            evidence_location=_step_location(steps, ("积分", "C", "+")) if steps else "",
        ))
    composed_function = bool(re.search(r"(?:sin|cos|tan|exp|ln)\([^()]*[a-zA-Z][^()]*\)", payload.standard_answer))
    missing_inner_factor = ("求导" in problem or "导数" in problem) and composed_function and re.search(r"\b[2-9][0-9]*\*x\b", payload.standard_answer) and not re.search(r"\b[2-9][0-9]*\*x\b", payload.student_answer)
    if missing_inner_factor:
        diagnoses.append(DiagnosisItem(
            code="CHAIN_RULE_OMITTED", label="链式法则遗漏", confidence=0.76,
            evidence="标准结果含复合函数的内层导数因子，但学生最终结果未识别到该因子。",
            next_step="把外层函数求导后，再乘以内层表达式的导数；检查括号内的 x 或系数是否被漏乘。",
            evidence_source="final_answer",
        ))

    if ("极值" in problem or "最大值" in problem or "最小值" in problem) and steps and not any(word in step_text for word in ("端点", "区间端点", "比较")):
        diagnoses.append(DiagnosisItem(
            code="ENDPOINT_CHECK_OMITTED", label="极值题未检查端点", confidence=0.74,
            evidence="题目要求区间上的极值，但补充步骤未出现端点代入或比较。",
            next_step="列出所有驻点及区间端点，分别代入原函数后再比较大小。",
            evidence_source="student_steps",
            evidence_location=_step_location(steps, ("端点", "驻点", "极值")),
        ))
    if ("定积分" in problem or "∫_" in problem) and "换元" in step_text and not any(word in step_text for word in ("上限", "下限", "积分限")):
        diagnoses.append(DiagnosisItem(
            code="SUBSTITUTION_BOUNDS_NOT_UPDATED", label="换元后积分上下限未同步", confidence=0.72,
            evidence="步骤提到换元，但未识别到积分上下限的同步处理。",
            next_step="换元后把原上下限代入新变量；或先求不定积分，再回代后代入原上下限。",
            evidence_source="student_steps",
            evidence_location=_step_location(steps, ("换元",)),
        ))

    if not diagnoses:
        return MisconceptionDiagnosisResult(
            success=True, confidence=0.35, diagnoses=[],
            summary="最终结果不一致，但仅凭最终答案无法可靠定位具体错因。",
            evidence=["未发现可由规则直接验证的符号、幂次、函数或定义域错误。"],
            warnings=["请让学生补充关键中间步骤，再进行针对性诊断。"],
            error_code="NEED_INTERMEDIATE_STEPS",
        )

    return MisconceptionDiagnosisResult(
        success=True,
        confidence=max(item.confidence for item in diagnoses),
        diagnoses=diagnoses,
        summary="已识别到可验证的典型错误：" + "、".join(item.label for item in diagnoses) + "。",
        evidence=[item.evidence for item in diagnoses],
    )
