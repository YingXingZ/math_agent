"""调用本地 Qwen 的独立求解 Skill。"""
from __future__ import annotations

import json
import os
import urllib.request

from .registry import registry
from .schemas import IndependentSolveInput, IndependentSolveResult


@registry.register("independent_solving", version="1.0.0", config={"provider": "local_qwen", "timeout_seconds": 600})
def independent_solving(payload: IndependentSolveInput, *, timeout_seconds: int = 600) -> IndependentSolveResult:
    """让本地 Qwen 只依据题目独立求解；标准答案永不进入请求。"""
    url = os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080").rstrip("/") + "/solve"
    request_body = {
        "problem_text": payload.problem_text,
        "section_no": payload.section_no or "",
        "problem_no": payload.problem_no or "",
    }
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, dict):
            return IndependentSolveResult(
                success=False,
                warnings=["Qwen 返回了非对象格式，不能作为可靠求解依据。"],
                error_code="INVALID_MODEL_RESPONSE",
            )
        answer = str(raw.get("std_answer") or "").strip() or None
        if not answer:
            return IndependentSolveResult(
                success=False,
                model_name=str(raw.get("model") or "local-qwen"),
                raw_response=raw,
                warnings=["Qwen 未返回可用于交叉验证的最终答案。"],
                error_code="MISSING_FINAL_ANSWER",
            )
        return IndependentSolveResult(
            success=True,
            confidence=float(raw.get("confidence") or 0),
            answer=answer,
            full_solution=str(raw.get("full_solution") or "").strip() or None,
            model_name=str(raw.get("model") or "local-qwen"),
            raw_response=raw,
            evidence=["本地模型根据题目独立求解；请求中未包含标准答案。"],
        )
    except TimeoutError:
        return IndependentSolveResult(
            success=False,
            warnings=["本地 Qwen 求解超时。"],
            error_code="MODEL_TIMEOUT",
        )
    
    except Exception as exc:
        return IndependentSolveResult(
            success=False,
            warnings=["本地 Qwen 独立求解服务不可用。"],
            error_code="MODEL_UNAVAILABLE",
            raw_response={"detail": str(exc)[:200]},
        )

    
