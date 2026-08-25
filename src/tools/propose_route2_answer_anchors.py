"""Propose Route2 answer-PDF page anchors from native text, without OCR or content edits.

The tool never alters ``problems``.  ``--apply`` only inserts provenance rows
with status ``candidate`` for records that satisfy the conservative evidence
gate; every proposed anchor remains subject to teacher confirmation.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench8014.source_evidence import ensure_source_evidence_schema  # noqa: E402


SECTION_RE = re.compile(r"(?m)^\s*习题\s*(5\.[1-6])")
TOTAL_EXERCISE_RE = re.compile(r"总习题五")
NUMBER_RE = re.compile(r"^\s*(\d{1,2}|[lI])\s*[.．、]")


def compact(value: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", value).lower()


def similarity(source_text: str, stored_stem: str) -> float:
    """Compare only a short prompt prefix; formulas are too OCR-sensitive."""
    left, right = compact(source_text)[:100], compact(stored_stem)[:100]
    if not left or not right:
        return 0.0
    return round(difflib.SequenceMatcher(None, left, right).ratio(), 4)


def page_sections(document: fitz.Document) -> dict[str, tuple[int, int]]:
    starts: list[tuple[str, int]] = []
    for page_index, page in enumerate(document):
        match = SECTION_RE.search(page.get_text("text"))
        if match:
            starts.append((match.group(1), page_index))
    result: dict[str, tuple[int, int]] = {}
    for index, (section_no, start) in enumerate(starts):
        end = starts[index + 1][1] - 1 if index + 1 < len(starts) else len(document) - 1
        result[section_no] = (start, end)
    # The final section is followed by "总习题五", not another "习题5.x"
    # heading.  Without this guard, repeated numbers in later chapters could
    # be falsely attributed to §5.6.
    if "5.6" in result:
        start, end = result["5.6"]
        for page_index in range(start, end + 1):
            if TOTAL_EXERCISE_RE.search(document[page_index].get_text("text")):
                result["5.6"] = (start, page_index)
                break
    return result


def numbered_blocks(document: fitz.Document, start: int, end: int) -> dict[int, tuple[int, list[float], str]]:
    """Return prompt blocks expanded only to the next question on the same page.

    Cross-page continuations intentionally remain separate candidates: joining a
    following page without an exact boundary risks capturing another question.
    """
    found: dict[int, tuple[int, list[float], str]] = {}
    for page_index in range(start, end + 1):
        numbered: list[tuple[int, tuple]] = []
        for block in document[page_index].get_text("blocks"):
            text = str(block[4]).strip()
            match = NUMBER_RE.match(text)
            if not match:
                continue
            token = match.group(1)
            number = 1 if token in {"l", "I"} else int(token)
            numbered.append((number, block))
        numbered.sort(key=lambda item: (float(item[1][1]), float(item[1][0])))
        for index, (number, block) in enumerate(numbered):
            # For the final numbered question, the safe same-page boundary is
            # the page footer.  Keeping only its first text block produced a
            # title-only 13pt crop and an empty OCR result.  We still never
            # cross into the following page, where the next question may be.
            next_top = (float(numbered[index + 1][1][1]) if index + 1 < len(numbered)
                        else float(document[page_index].rect.height - 18))
            bottom = max(float(block[3]), next_top - 3)
            bbox = [round(float(block[0]), 2), round(float(block[1]), 2),
                    round(float(document[page_index].rect.width - 18), 2), round(bottom, 2)]
            # Keep the first block only: in a worked solution later enumerations
            # commonly begin with (1), but not with an exercise-number token.
            found.setdefault(number, (page_index, bbox, str(block[4]).strip()))
    return found


def load_route2_problems(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT p.id AS problem_id,p.problem_no,p.content_text,s.section_no,td.id AS document_id
        FROM problems p
        JOIN sections s ON s.id=p.section_id
        JOIN textbook_documents td ON td.textbook_id=s.textbook_id
        WHERE td.document_role='route2_question_answer_ocr' AND s.section_no IN ('5.1','5.2','5.3','5.4','5.5','5.6')
        ORDER BY s.section_no, CAST(p.problem_no AS INTEGER), p.sub_no
    """).fetchall()


def build_candidates(db_path: Path, pdf_path: Path) -> tuple[list[dict], list[dict]]:
    conn = sqlite3.connect(db_path)
    try:
        ensure_source_evidence_schema(conn)
        problems = load_route2_problems(conn)
    finally:
        conn.close()
    by_section: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for problem in problems:
        by_section[str(problem["section_no"])].append(problem)
    document = fitz.open(pdf_path)
    try:
        ranges = page_sections(document)
        candidates, blocked = [], []
        for section_no, section_problems in by_section.items():
            if section_no not in ranges:
                blocked.extend({"problem_id": row["problem_id"], "section_no": section_no,
                                "problem_no": row["problem_no"], "reason": "section_header_not_found"} for row in section_problems)
                continue
            positions = numbered_blocks(document, *ranges[section_no])
            for row in section_problems:
                try:
                    problem_no = int(str(row["problem_no"]))
                except ValueError:
                    blocked.append({"problem_id": row["problem_id"], "section_no": section_no,
                                    "problem_no": row["problem_no"], "reason": "non_numeric_problem_no"})
                    continue
                match = positions.get(problem_no)
                if not match:
                    blocked.append({"problem_id": row["problem_id"], "section_no": section_no,
                                    "problem_no": row["problem_no"], "reason": "numbered_prompt_not_found"})
                    continue
                page_index, bbox, source_text = match
                score = similarity(source_text, str(row["content_text"] or ""))
                stem_hash = hashlib.sha256(str(row["content_text"] or "").encode("utf-8")).hexdigest()
                item = {"problem_id": row["problem_id"], "document_id": row["document_id"],
                        "section_no": section_no, "problem_no": problem_no, "pdf_page_index": page_index,
                        "pdf_page_number": page_index + 1, "bbox": bbox,
                        "source_prompt_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                        "stored_stem_sha256": stem_hash, "prompt_similarity": score,
                        "resolution_method": "question_number_to_next_native_text",
                        "status": "candidate" if score >= 0.58 else "needs_teacher",
                        "review_reason": "section_header+question_number+next_question_boundary+prompt_similarity" if score >= 0.58 else "question_number_found_but_prompt_similarity_below_gate"}
                candidates.append(item)
        return candidates, blocked
    finally:
        document.close()


def apply_candidates(db_path: Path, candidates: list[dict]) -> int:
    accepted = [row for row in candidates if row["status"] == "candidate"]
    conn = sqlite3.connect(db_path)
    try:
        ensure_source_evidence_schema(conn)
        now = datetime.now(timezone.utc).isoformat()
        for row in accepted:
            conn.execute("""
                INSERT INTO problem_source_anchors
                (problem_id,document_id,pdf_page_index,bbox_json,bbox_space,segment_index,
                 resolution_method,confidence,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(problem_id,document_id,pdf_page_index,segment_index) DO UPDATE SET
                  bbox_json=excluded.bbox_json,bbox_space=excluded.bbox_space,
                  resolution_method=excluded.resolution_method,confidence=excluded.confidence,
                  status=excluded.status,updated_at=excluded.updated_at
            """, (row["problem_id"], row["document_id"], row["pdf_page_index"],
                  json.dumps(row["bbox"]), "pdf_points", 0, row["resolution_method"],
                  row["prompt_similarity"], "candidate", now, now))
        conn.commit()
        return len(accepted)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Insert only gated rows as candidate anchors")
    args = parser.parse_args()
    candidates, blocked = build_candidates(args.db, args.pdf)
    accepted = apply_candidates(args.db, candidates) if args.apply else 0
    payload = {"schema_version": "route2-anchor-review/v1",
               "created_at": datetime.now(timezone.utc).isoformat(),
               "mode": "applied" if args.apply else "dry-run",
               "counts": {"total": len(candidates), "candidate": sum(x["status"] == "candidate" for x in candidates),
                          "needs_teacher": sum(x["status"] != "candidate" for x in candidates),
                          "blocked": len(blocked), "applied": accepted},
               "candidates": candidates, "blocked": blocked}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
