"""Deterministic answer matching and the 90% publication gate.

The core business rule is deliberately model-free:
    canonical(section_no) + canonical(question_no) + canonical(sub_no)

A result below the configured threshold is *blocked* and must enter the existing
MinerU teacher review queue.  This module never writes the authoritative 8014 DB.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
import re
import unicodedata

DEFAULT_THRESHOLD = 0.90


def canonical_section(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"^习题\s*", "", text)
    text = re.sub(r"\s+", "", text)
    return text.replace("-", ".").replace("．", ".").replace("。", ".")


def canonical_no(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return str(int(text)) if text.isdigit() else text


def match_key(section_no: object, question_no: object, sub_no: object = None) -> str:
    section = canonical_section(section_no)
    question = canonical_no(question_no)
    sub = canonical_no(sub_no) if sub_no not in (None, "") else ""
    return f"{section}/{question}" + (f"({sub})" if sub else "")


def answer_json_to_staged(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt server_answer_parse.py JSON to mineru_review.create_session contract."""
    if payload.get("status") != "ok" or not (payload.get("meta") or {}).get("found"):
        raise ValueError("答案解析结果未成功定位目标小节，不能进入匹配")
    meta = payload.get("meta") or {}
    section_no = canonical_section(meta.get("target_section"))
    items = []
    for problem in payload.get("problems") or []:
        text = str(problem.get("answer_text") or "").strip()
        # Parent-level exact match is the publish unit currently used by 8014 §1.1.
        # Preserve sub-items inside the candidate text; do not create duplicate rows.
        sub_text = " ".join(
            f"({sub.get('sub_no')}) {str(sub.get('answer_text') or '').strip()}"
            for sub in (problem.get("sub_items") or [])
        ).strip()
        candidate = " ".join(part for part in (text, sub_text) if part).strip()
        source = {
            "page": problem.get("source_page"),
            "pages": problem.get("source_pages") or [problem.get("source_page")],
            "bbox": problem.get("bbox"),
            "source_image": problem.get("source_image"),
        }
        quality_issues = []
        if not candidate:
            quality_issues.append("EMPTY_ANSWER")
        if not problem.get("source_image"):
            quality_issues.append("NO_SOURCE_IMAGE")
        score = max(0.0, 1.0 - 0.35 * len(quality_issues))
        no = canonical_no(problem.get("problem_no"))
        items.append({
            "item_id": match_key(section_no, no),
            "section_no": section_no,
            "question_no": no,
            "subquestion_no": None,
            "text": candidate,
            "answer_text": text,
            "sub_items": problem.get("sub_items") or [],
            "source": source,
            "quality": {"score": round(score, 2), "issues": quality_issues,
                        "pass": not quality_issues},
            "review_status": "pending",
        })
    return {
        "schema_version": "math-knowledge-staging/v2",
        "status": "staged",
        "temporary": True,
        "document": {
            "name": meta.get("source_pdf_name") or "MinerU 答案书",
            "role": "answer_book",
            "source_pdf": meta.get("source_pdf"),
            "generated_at": meta.get("generated_at"),
        },
        "quality_report": {
            "section_count": 1,
            "item_count": len(items),
            "valid_item_count": sum(bool(item["quality"]["pass"]) for item in items),
        },
        "sections": [{
            "section_no": section_no,
            "title": (payload.get("section") or {}).get("title") or f"习题{section_no}",
            "items": items,
        }],
        "errors": [],
    }


def match_section_questions(section_no: str, questions: list[dict[str, Any]],
                            answer_book: dict[str, Any],
                            threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    """Match 8014 rows to staged answers and enforce a hard publish gate."""
    if not 0 < threshold <= 1:
        raise ValueError("threshold 必须在 (0, 1] 区间")
    target_section = canonical_section(section_no)
    answers = [
        item for section in (answer_book.get("sections") or [])
        if canonical_section(section.get("section_no")) == target_section
        for item in (section.get("items") or [])
    ]
    answer_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for answer in answers:
        answer_index[match_key(answer.get("section_no"), answer.get("question_no"),
                               answer.get("subquestion_no"))].append(answer)

    matches = []
    used_answer_ids: set[int] = set()
    for question in questions:
        key = match_key(target_section, question.get("problem_no"), question.get("sub_no"))
        candidates = answer_index.get(key, [])
        reasons: list[str] = []
        if len(candidates) == 1:
            answer = candidates[0]
            used_answer_ids.add(id(answer))
            quality_ok = bool((answer.get("quality") or {}).get("pass"))
            status = "auto_match" if quality_ok else "need_review"
            confidence = 1.0 if quality_ok else float((answer.get("quality") or {}).get("score", 0))
            if not quality_ok:
                reasons.append("ANSWER_QUALITY_GATE")
        elif len(candidates) > 1:
            answer, status, confidence = None, "need_review", 0.0
            reasons.append("DUPLICATE_ANSWER_KEY")
        else:
            answer, status, confidence = None, "unmatched", 0.0
            reasons.append("ANSWER_NOT_FOUND")
        matches.append({
            "match_key": key,
            "source_problem_id": question.get("source_problem_id") or question.get("id"),
            "section_no": target_section,
            "question_no": canonical_no(question.get("problem_no")),
            "subquestion_no": question.get("sub_no"),
            "status": status,
            "confidence": round(confidence, 2),
            "answer_item": answer,
            "evidence": {
                "question_source_image": (question.get("evidence") or {}).get("crop_image_path"),
                "answer_source": (answer or {}).get("source"),
            },
            "reasons": reasons,
        })

    total = len(matches)
    auto_count = sum(item["status"] == "auto_match" for item in matches)
    review_count = sum(item["status"] == "need_review" for item in matches)
    unmatched_count = sum(item["status"] == "unmatched" for item in matches)
    match_rate = auto_count / total if total else 0.0
    orphan_answers = [
        item["item_id"] for item in answers if id(item) not in used_answer_ids
    ]
    can_publish = total > 0 and match_rate >= threshold and review_count == 0 and unmatched_count == 0
    return {
        "status": "ready" if can_publish else "review_required",
        "temporary": True,
        "matching_rule": "section_no + question_no + subquestion_no",
        "publish_gate": {
            "threshold": threshold,
            "match_rate": round(match_rate, 4),
            "can_publish": can_publish,
            "action": "allow_publish" if can_publish else "block_and_create_review",
            "reason": None if can_publish else (
                f"精确匹配率 {match_rate:.1%}，低于阈值 {threshold:.1%} 或存在待复核/未匹配项"
            ),
        },
        "summary": {
            "questions": total,
            "answers": len(answers),
            "auto_match": auto_count,
            "need_review": review_count,
            "unmatched": unmatched_count,
            "orphan_answers": len(orphan_answers),
        },
        "matches": matches,
        "orphan_answer_ids": orphan_answers,
    }
