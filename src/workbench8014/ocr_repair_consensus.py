"""Conservative, non-writing consensus for mathematical OCR candidates."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

DECISIONS = {"AUTO_ACCEPT", "AUTO_REPAIR", "NEEDS_TEACHER_REVIEW"}
_SPACE = re.compile(r"\s+")
_LATEX_SPACE = re.compile(r"\s*(\^|_|\{|\}|\\,|\\!|\\left|\\right)\s*")
_CRITICAL = re.compile(r"<=|>=|!=|≤|≥|≠|∈|∉|∪|∩|[<>+=−-]|\\(?:leq|geq|neq|in|notin|cup|cap|int|frac|sqrt)|\^\s*\{?[^\s}]+|_\s*\{?[^\s}]+")


def normalise(value: str) -> str:
    value = (value or "").replace("\\displaystyle", "").replace("−", "-")
    value = _LATEX_SPACE.sub(r"\1", value)
    # Braces around single limits and superficial spacing have no OCR meaning.
    # This is comparison-only; the original candidate is always preserved.
    return _SPACE.sub("", value.replace("{", "").replace("}", "")).strip()


def critical_tokens(value: str) -> tuple[str, ...]:
    return tuple(_CRITICAL.findall(normalise(value)))


def decide(candidates: Iterable[dict], minimum_confidence: float = 0.75) -> dict:
    """Return a recommendation only; no caller is authorised to write a problem.

    Two independent providers must match after normalisation and every matching
    candidate needs enough stated confidence with no declared risk.  A conflict
    in a critical mathematical token always requires a teacher.
    """
    usable = []
    for item in candidates:
        text = str(item.get("latex_text") or item.get("text") or "")
        confidence = float(item.get("confidence") or 0)
        risks = item.get("risks") or []
        if text and not risks:
            usable.append({**item, "normalised": normalise(text), "critical_tokens": critical_tokens(text), "confidence": confidence})
    providers = {str(x.get("provider") or "") for x in usable}
    if len(providers) < 2:
        return {"decision": "NEEDS_TEACHER_REVIEW", "reason": "fewer_than_two_independent_available_providers", "matched_providers": []}
    groups: dict[str, list[dict]] = {}
    for item in usable:
        groups.setdefault(item["normalised"], []).append(item)
    winner = max(groups.values(), key=len)
    winner_providers = sorted({str(item.get("provider")) for item in winner})
    if len(winner_providers) < 2 or any(item["confidence"] < minimum_confidence for item in winner):
        return {"decision": "NEEDS_TEACHER_REVIEW", "reason": "no_high_confidence_independent_agreement", "matched_providers": winner_providers}
    other_critical = {token for group in groups.values() for item in group if group is not winner for token in item["critical_tokens"]}
    if other_critical and any(item["critical_tokens"] != winner[0]["critical_tokens"] for group in groups.values() if group is not winner for item in group):
        return {"decision": "NEEDS_TEACHER_REVIEW", "reason": "critical_math_token_conflict", "matched_providers": winner_providers}
    decision = "AUTO_ACCEPT" if len(groups) == 1 else "AUTO_REPAIR"
    return {"decision": decision, "reason": "independent_normalised_agreement", "matched_providers": winner_providers,
            "latex_candidate": winner[0]["normalised"], "critical_tokens": list(winner[0]["critical_tokens"])}
