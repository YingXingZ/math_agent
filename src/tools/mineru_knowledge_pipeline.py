#!/usr/bin/env python3
"""MinerU staging pipeline for textbook-question and answer-book matching.

This program is intentionally database-free. It turns MinerU Markdown into
reviewable JSON, then matches textbook and answer records by a canonical key:
section + question number + optional sub-question number.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "math-knowledge-staging/v1"
SECTION_RE = re.compile(r"(?mi)^\s*#{1,6}\s*(?:习题\s*)?(?:第\s*)?(\d+[.．]\d+)\s*(.*)$")
EXERCISE_SECTION_RE = re.compile(r"(?mi)^\s*#{1,6}\s*习题\s*(\d+[.．]\d+)\s*(.*)$")
QUESTION_RE = re.compile(r"(?m)^\s*(?:第\s*)?(\d{1,3})\s*[.、．：:]\s*")
SUBQUESTION_RE = re.compile(r"[（(]\s*(\d{1,2})\s*[）)]")
GARBLED_RE = re.compile(r"[锟鈫愇禲]|(?:[JHVUR]{2,})|(?:[VJH]\d)")


def norm_section(value: str) -> str:
    return re.sub(r"[．。]", ".", value).strip()


def norm_question(value: str) -> str:
    return str(int(value)) if value.isdigit() else value.strip()


def key(section: str, question_no: str, sub_no: str | None = None) -> str:
    return f"{norm_section(section)}/{norm_question(question_no)}" + (f"({sub_no})" if sub_no else "")


def quality(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    one_char_ratio = sum(len(line) <= 1 for line in lines) / max(1, len(lines))
    issues: list[str] = []
    if len(text.strip()) < 10:
        issues.append("TEXT_TOO_SHORT")
    if GARBLED_RE.search(text):
        issues.append("OCR_GARBLED")
    if len(lines) >= 7 and one_char_ratio >= 0.45:
        issues.append("VERTICAL_FRAGMENT")
    if len(text) > 30 and cjk < 3:
        issues.append("LOW_NATURAL_LANGUAGE_SIGNAL")
    score = max(0.0, 1.0 - 0.32 * len(issues))
    return {"score": round(score, 2), "issues": issues, "pass": not issues}


def heading_boundaries(markdown: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for hit in SECTION_RE.finditer(markdown):
        found.append((hit.start(), norm_section(hit.group(1)), hit.group(2).strip()))
    return found


def section_blocks(markdown: str) -> Iterable[tuple[str, str, int, str]]:
    boundaries = heading_boundaries(markdown)
    for index, (start, section_no, title) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(markdown)
        yield section_no, title, start, markdown[start:end]


def split_questions(section_no: str, section_text: str, section_offset: int) -> list[dict]:
    hits = list(QUESTION_RE.finditer(section_text))
    records: list[dict] = []
    for index, hit in enumerate(hits):
        end = hits[index + 1].start() if index + 1 < len(hits) else len(section_text)
        raw = section_text[hit.start():end].strip()
        no = norm_question(hit.group(1))
        # Keep a parent question plus each explicitly marked sub-question.
        subs = list(SUBQUESTION_RE.finditer(raw))
        if not subs:
            records.append(_item(section_no, no, None, raw, section_offset + hit.start(), section_offset + end))
            continue
        lead = raw[:subs[0].start()].strip()
        if lead:
            records.append(_item(section_no, no, None, lead, section_offset + hit.start(), section_offset + hit.start() + len(lead)))
        for sub_index, sub in enumerate(subs):
            sub_end = subs[sub_index + 1].start() if sub_index + 1 < len(subs) else len(raw)
            value = raw[sub.start():sub_end].strip()
            records.append(_item(section_no, no, sub.group(1), value, section_offset + hit.start() + sub.start(), section_offset + hit.start() + sub_end))
    return records


def _item(section_no: str, no: str, sub: str | None, text: str, start: int, end: int) -> dict:
    return {
        "item_id": key(section_no, no, sub),
        "section_no": norm_section(section_no),
        "question_no": no,
        "subquestion_no": sub,
        "text": text,
        "source": {"offset_start": start, "offset_end": end, "page": None, "bbox": None},
        "quality": quality(text),
        "review_status": "pending",
    }


def build_document(markdown_path: Path, role: str, document_name: str | None = None) -> dict:
    markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
    sections = []
    all_issues: list[str] = []
    for section_no, title, offset, text in section_blocks(markdown):
        items = split_questions(section_no, text, offset)
        if not items:
            all_issues.append(f"NO_QUESTION_NUMBER:{section_no}")
        sections.append({
            "section_no": section_no,
            "title": title or f"习题{section_no}",
            "source_offset_start": offset,
            "items": items,
        })
    item_count = sum(len(section["items"]) for section in sections)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "staged",
        "temporary": True,
        "document": {
            "name": document_name or markdown_path.stem,
            "role": role,
            "markdown_path": str(markdown_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "quality_report": {
            "section_count": len(sections),
            "item_count": item_count,
            "valid_item_count": sum(item["quality"]["pass"] for section in sections for item in section["items"]),
            "issues": all_issues,
        },
        "sections": sections,
        "errors": [] if sections else [{"code": "NO_SECTION_FOUND", "message": "No numbered exercise section was found."}],
    }


def items(document: dict) -> dict[str, dict]:
    return {item["item_id"]: item for section in document.get("sections", []) for item in section.get("items", [])}


def match(textbook: dict, answers: dict) -> dict:
    q_items, a_items = items(textbook), items(answers)
    matches = []
    for item_id, question in q_items.items():
        answer = a_items.get(item_id)
        if answer:
            q_ok, a_ok = question["quality"]["pass"], answer["quality"]["pass"]
            confidence = 1.0 if q_ok and a_ok else 0.72
            matches.append({
                "match_id": item_id,
                "question_id": item_id,
                "answer_id": item_id,
                "status": "auto_pass" if confidence >= .9 else "needs_review",
                "confidence": confidence,
                "evidence": {"match_key": item_id, "question_source": question["source"], "answer_source": answer["source"]},
                "reasons": [] if confidence >= .9 else ["QUALITY_GATE"],
            })
        else:
            matches.append({"match_id": item_id, "question_id": item_id, "answer_id": None, "status": "unmatched", "confidence": 0.0, "evidence": {"match_key": item_id}, "reasons": ["ANSWER_NOT_FOUND"]})
    orphan_answers = [item_id for item_id in a_items if item_id not in q_items]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "staged",
        "temporary": True,
        "matching_rule": "section_no + question_no + subquestion_no",
        "summary": {
            "questions": len(q_items), "answers": len(a_items), "auto_pass": sum(m["status"] == "auto_pass" for m in matches),
            "needs_review": sum(m["status"] == "needs_review" for m in matches), "unmatched": sum(m["status"] == "unmatched" for m in matches),
            "orphan_answers": len(orphan_answers),
        },
        "matches": matches,
        "orphan_answer_ids": orphan_answers,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--markdown", type=Path, required=True)
    stage.add_argument("--role", choices=("textbook", "answer_book"), required=True)
    stage.add_argument("--output", type=Path, required=True)
    join = sub.add_parser("match")
    join.add_argument("--textbook", type=Path, required=True)
    join.add_argument("--answers", type=Path, required=True)
    join.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "stage":
        result = build_document(args.markdown, args.role)
    else:
        result = match(json.loads(args.textbook.read_text(encoding="utf-8")), json.loads(args.answers.read_text(encoding="utf-8")))
    write_json(args.output, result)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
