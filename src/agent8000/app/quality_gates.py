"""Deterministic quality gates shared by grading and release checks."""
from __future__ import annotations

import json
import re
from typing import Any
from .reference_tools import numbered_parts


def audit_rubric(problem_text: str, standard_answer: str, rubric: str, max_score: float) -> dict[str, Any]:
    """Validate a rubric and normalize relative point weights to this question."""
    issues: list[str] = []
    parsed: Any = None
    raw = str(rubric or "").strip()
    normalized = False
    source_total: float | None = None
    target_total = float(max_score)
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            issues.append("评分标准 JSON 无法解析")
    if isinstance(parsed, list) and parsed:
        try:
            source_total = sum(float(item.get("weight") or item.get("max_score") or 0) for item in parsed)
            if source_total <= 0:
                issues.append("评分点分值结构无效")
            elif abs(source_total - target_total) > 0.02:
                scale = target_total / source_total
                scaled: list[dict[str, Any]] = []
                for raw_item in parsed:
                    item = dict(raw_item)
                    key = "weight" if item.get("weight") is not None else "max_score"
                    item[key] = round(float(item.get(key) or 0) * scale, 3)
                    scaled.append(item)
                parsed = scaled
                raw = json.dumps(parsed, ensure_ascii=False)
                normalized = True
        except (AttributeError, TypeError, ValueError):
            issues.append("评分点分值结构无效")

    expected = numbered_parts(problem_text)
    answers = set(numbered_parts(standard_answer))
    if len(expected) >= 2:
        missing = [part for part in expected if part not in answers]
        if missing:
            issues.append("标准答案缺少第 " + "、".join(missing) + " 问")
        if isinstance(parsed, list):
            covered = set(re.findall(r"[\uff08(]\s*(\d+)\s*[)\uff09]", json.dumps(parsed, ensure_ascii=False)))
            if any(part not in covered for part in expected):
                issues.append("结构化评分点未覆盖全部编号小问")
    return {
        "valid": not issues,
        "issues": issues,
        "source": "rubric" if raw and not issues else "standard_answer_fallback",
        "effective_solution": raw if raw and not issues else str(standard_answer or ""),
        "normalized": normalized,
        "source_total": source_total,
        "target_total": target_total,
    }


def gate_unverified_candidate(qwen: dict[str, Any], *, answer_present: bool | None,
                              trusted_reference: bool) -> bool:
    """Never surface positive credit without paper and approved reference evidence."""
    if (answer_present is True and qwen.get('independent_ocr')
            and qwen.get('provisional_reference') and not qwen.get('reference_incomplete')):
        qwen['need_review'] = True
        return False
    if answer_present is not None and (trusted_reference or answer_present is False):
        return False
    try:
        qwen["unverified_candidate_score"] = float(qwen.get("score") or 0)
    except (TypeError, ValueError):
        qwen["unverified_candidate_score"] = None
    qwen["score"] = 0.0
    qwen["correct"] = None
    qwen["step_scores"] = []
    qwen["need_review"] = True
    try:
        qwen["confidence"] = min(float(qwen.get("confidence") or 0), 0.49)
    except (TypeError, ValueError):
        qwen["confidence"] = 0.0
    qwen["feedback"] = "作答区域或教师评分依据未确认，当前不产生可采纳的候选分；请教师核对原图与参考解。"
    return True


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


def reconcile_step_scores_to_question_score(model_result: dict[str, Any], max_score: float) -> dict[str, Any]:
    """Keep visible step scores consistent with the final per-question score.

    The VLM may provide a useful step breakdown and then have its aggregate score
    corrected by deterministic answer/rubric checks.  The correction must be
    reflected in the evidence shown to teachers; otherwise a full-score question
    can misleadingly display partial-credit steps.
    """
    steps = model_result.get("step_scores")
    try:
        target = max(0.0, min(float(max_score), float(model_result.get("score") or 0)))
    except (TypeError, ValueError):
        return {"reconciled": False, "reason": "invalid_question_score"}
    if not isinstance(steps, list) or not steps:
        return {"reconciled": False, "reason": "no_steps", "target_score": target}
    try:
        maxima = [max(0.0, float(step.get("max_score") or 0)) for step in steps]
        awarded = [max(0.0, min(float(step.get("score") or 0), maximum)) for step, maximum in zip(steps, maxima)]
    except (AttributeError, TypeError, ValueError):
        return {"reconciled": False, "reason": "invalid_steps", "target_score": target}
    total_max, source_total = sum(maxima), sum(awarded)
    if total_max <= 0:
        return {"reconciled": False, "reason": "zero_total", "target_score": target}
    # normalize_step_scores has already aligned maxima; keep this helper safe if
    # it is reused independently.
    target = min(target, total_max)
    if abs(source_total - target) <= 0.02:
        return {"reconciled": False, "reason": "already_aligned", "target_score": target, "source_total": source_total}
    if target >= total_max - 0.02:
        adjusted = list(maxima)
        mode = "full_credit"
    elif source_total > 0:
        scale = target / source_total
        adjusted = [min(maximum, value * scale) for value, maximum in zip(awarded, maxima)]
        mode = "proportional"
    else:
        # No individual award was supplied although an aggregate score exists.
        # Preserve the maximum-point structure and make the adjustment explicit.
        adjusted = [target * maximum / total_max for maximum in maxima]
        mode = "by_weight"
    adjusted = [round(value, 3) for value in adjusted]
    residual = round(target - sum(adjusted), 3)
    if abs(residual) >= 0.001:
        for index in range(len(adjusted) - 1, -1, -1):
            candidate = round(adjusted[index] + residual, 3)
            if 0 <= candidate <= round(maxima[index], 3):
                adjusted[index] = candidate
                break
    for step, score in zip(steps, adjusted):
        step["score"] = score
    return {
        "reconciled": True,
        "reason": "aggregate_score_mismatch",
        "mode": mode,
        "target_score": target,
        "source_total": source_total,
        "final_total": round(sum(adjusted), 3),
    }


def audit_candidate_breakdown(model_result: dict[str, Any], max_score: float) -> dict[str, Any]:
    """Verify that visible candidate scoring points are self-consistent.

    Candidate scoring points are evidence, not a replacement for the teacher.
    An invalid breakdown remains visible but is forced to teacher review.
    """
    target_max = round(float(max_score), 3)
    issues: list[str] = []
    steps = model_result.get("step_scores")
    try:
        target_score = round(max(0.0, min(target_max, float(model_result.get("score") or 0))), 3)
    except (TypeError, ValueError):
        target_score = 0.0
        issues.append("本题建议分不是有效数字")
    if not isinstance(steps, list) or not steps:
        issues.append("未生成可核对的评分点候选")
        return {"valid": False, "issues": issues, "candidate_max_total": 0.0,
                "candidate_score_total": 0.0, "question_max_score": target_max,
                "question_score": target_score}
    maxima: list[float] = []
    awarded: list[float] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not str(step.get("step") or "").strip():
            issues.append(f"第 {index} 个评分点缺少说明")
            continue
        try:
            maximum = round(float(step.get("max_score") or 0), 3)
            score = round(float(step.get("score") or 0), 3)
        except (TypeError, ValueError):
            issues.append(f"第 {index} 个评分点分值无效")
            continue
        if maximum <= 0:
            issues.append(f"第 {index} 个评分点满分必须大于 0")
        if score < -0.001 or score > maximum + 0.001:
            issues.append(f"第 {index} 个评分点得分超出范围")
        maxima.append(maximum)
        awarded.append(score)
    candidate_max_total = round(sum(maxima), 3)
    candidate_score_total = round(sum(awarded), 3)
    if abs(candidate_max_total - target_max) > 0.02:
        issues.append(f"评分点满分合计 {candidate_max_total:g} 与本题满分 {target_max:g} 不一致")
    if abs(candidate_score_total - target_score) > 0.02:
        issues.append(f"评分点得分合计 {candidate_score_total:g} 与本题建议分 {target_score:g} 不一致")
    return {"valid": not issues, "issues": issues,
            "candidate_max_total": candidate_max_total,
            "candidate_score_total": candidate_score_total,
            "question_max_score": target_max, "question_score": target_score}
