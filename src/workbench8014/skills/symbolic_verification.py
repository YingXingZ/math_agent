"""使用 SymPy 的确定性表达式判等 Skill。"""
from __future__ import annotations
import re

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from grading_engine import expr_equal, normalize_expr

from .registry import registry
from .schemas import SymbolicVerificationInput, VerificationResult


@registry.register("symbolic_verification", version="1.1.0", config={"engine": "sympy", "deterministic": True})
def symbolic_verification(payload: SymbolicVerificationInput) -> VerificationResult:
    """判定学生答案与标准答案是否数学等价，绝不调用模型。"""
    # OCR 的“答案见图”“证明略”等自然语言不能被 SymPy 当作未知变量高置信度处理。
    if re.search(r"[\u4e00-\u9fff]", payload.student_answer):
        return VerificationResult(
            success=False,
            correct=None,
            confidence=0.30,
            method="检测到非数学文本",
            evidence=["学生作答包含中文说明或无法辨认文本，不能进行符号判等。"],
            warnings=["需要调用独立求解/视觉识别，或转教师复核。"],
            error_code="NON_MATHEMATICAL_ANSWER",
        )
    correct, confidence, method = expr_equal(payload.student_answer, payload.standard_answer)
    return VerificationResult(
        success=True,
        correct=correct,
        confidence=confidence,
        method=method,
        normalized_student_answer=normalize_expr(payload.student_answer) or None,
        # 不把标准答案或其归一化形式放进返回值，避免经 API 泄露。
        evidence=[f"判定方法：{method}"],
        warnings=[] if confidence >= 0.85 else ["表达式无法被可靠解析，需要独立求解或教师复核。"],
        error_code=None if confidence >= 0.85 else "LOW_CONFIDENCE_VERIFICATION",
    )
