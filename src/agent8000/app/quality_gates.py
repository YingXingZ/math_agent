"""Deterministic quality gates shared by grading and release checks."""
from __future__ import annotations

import json
import re
from typing import Any


def audit_rubric(problem_text: str, standard_answer: str, rubric: str, max_score: float) -> dict[str, Any]:
    issues: list[str] = []
    parsed: Any = None
    raw = str(rubric or "").strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            issues.append("评分标准 JSON 无法解析")
    if isinstance(parsed, list) and parsed:
        try:
            total = sum(float(item.get("weight") or item.get("max_score") or 0) for item in parsed)
            if total <= 0 or abs(total - float(max_score)) > 0.02:
                issues.append(f"评分点合计 {total:g} 与题目满分 {float(max_score):g} 不一致")
        except (AttributeError, TypeError, ValueError):
            issues.append("评分点分值结构无效")

    expected = list(dict.fromkeys(re.findall(r"(?m)^\s*[（(]\s*(\d+)\s*[)）]", str(problem_text or ""))))
    answers = set(re.findall(r"(?m)^\s*[（(]\s*(\d+)\s*[)）]", str(standard_answer or "")))
    if len(expected) >= 2:
        missing = [part for part in expected if part not in answers]
        if missing:
            issues.append("标准答案缺少第 " + "、".join(missing) + " 问")
        if isinstance(parsed, list):
            covered = set(re.findall(r"[（(]\s*(\d+)\s*[)）]", json.dumps(parsed, ensure_ascii=False)))
            if any(part not in covered for part in expected):
                issues.append("结构化评分点未覆盖全部编号小问")
    return {
        "valid": not issues,
        "issues": issues,
        "source": "rubric" if raw and not issues else "standard_answer_fallback",
        "effective_solution": raw if raw and not issues else str(standard_answer or ""),
    }


def normalize_step_scores(model_result: dict[str, Any], max_score: float) -> dict[str, Any]:
    steps = model_result.get("step_scores")
    if not isinstance(steps, list) or not steps:
        return {"normalized": False, "reason": "no_steps"}
    try:
        source_total = sum(max(0.0, float(step.get("max_score") or 0)) for step in steps)
    except (AttributeError, TypeError, ValueError):
        return {"normalized": False, "reason": "invalid_steps"}
    if source_total <= 0:
        return {"normalized": False, "reason": "zero_total"}
    if abs(source_total - float(max_score)) <= 0.02:
        return {"normalized": False, "reason": "already_aligned", "source_total": source_total}
    scale = float(max_score) / source_total
    for step in steps:
        step_max = max(0.0, float(step.get("max_score") or 0)) * scale
        step_score = max(0.0, float(step.get("score") or 0)) * scale
        step["max_score"] = round(step_max, 3)
        step["score"] = round(min(step_score, step_max), 3)
    return {"normalized": True, "reason": "step_total_mismatch", "source_total": source_total,
            "target_total": float(max_score), "scale": scale}
