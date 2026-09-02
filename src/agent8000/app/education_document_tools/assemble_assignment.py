"""Tool 3: assemble a homework assignment from cached, stratified problems.

Pre-conditions (enforced by the Orchestrator / upstream tools):
    * the section(s) have already been ``sync``-ed from 8014 into the local
      cache (``review_status='published'``);
    * each published problem already carries a 基础 / 提高 / 综合 tier
      (``stratify_section_difficulty`` fills any missing tier heuristically).

This tool then:
    * selects ``question_count`` problems following the required 3-2-1
      distribution (基础 × round(n·basic_ratio), 提高 × round(n·advanced_ratio),
      the rest 综合);
    * preserves the *original* textbook problem number (设计二：保持原本题号);
    * inserts the assignment + assignment_questions rows;
    * optionally renders the printable A4 PDF + records the page count.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import connection
from ..assignment_pdf import build_assignment_pdf


_SUBPART_RE = re.compile(r"(?<![A-Za-z0-9])[（(]\s*\d+\s*[)）]")


def _source_locator(row: dict[str, Any], fallback: int) -> str:
    """Stable source reference retained across mixed-chapter assignments."""
    chapter = str(row.get("chapter") or "").strip()
    no = str(row.get("source_problem_no") or fallback).strip()
    if "第" in no and "题" in no:
        return no
    return f"{chapter} 第{no}题".strip() if chapter else f"第{no}题"


def _subpart_count(content: str) -> int:
    """Estimate visible sub-questions without splitting their answer/rubric.

    A source record stores one answer and one rubric for the whole textbook
    question. Until those three fields can be split together, selecting just
    one sub-part would make grading unreliable. Prefer complete questions
    with fewer sub-parts instead; this is a safe, explicit selection rule.
    """
    return max(1, len(_SUBPART_RE.findall(content or "")))


def _split_numbered_parts(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """Split a numbered question/answer/rubric, retaining its shared preface."""
    markers = list(_SUBPART_RE.finditer(text or ""))
    if len(markers) < 2:
        return None
    preface = (text or "")[:markers[0].start()].strip()
    parts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, marker in enumerate(markers):
        number = re.search(r"\d+", marker.group(0)).group(0)
        if number in seen:
            return None
        seen.add(number)
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text or "")
        parts.append((number, (text or "")[marker.start():end].strip()))
    return preface, parts


def _partial_variant(row: dict[str, Any], subpart_limit: int = 3) -> dict[str, Any] | None:
    """Create a safely aligned odd-numbered sub-question variant.

    Partial selection is permitted only when stem, standard answer and rubric
    all expose exactly the same numbered sub-parts.  Otherwise the parent
    question remains intact, so no answer key is ever mismatched to a stem.
    """
    stem = _split_numbered_parts(str(row.get("content") or ""))
    answer = _split_numbered_parts(str(row.get("answer") or ""))
    rubric = _split_numbered_parts(str(row.get("rubric") or ""))
    if not stem or not answer or not rubric:
        return None
    numbers = [n for n, _ in stem[1]]
    if len(numbers) < 3 or numbers != [n for n, _ in answer[1]] or numbers != [n for n, _ in rubric[1]]:
        return None
    take = min(max(3, int(subpart_limit or 3)), 6, len(numbers))
    if take >= len(numbers):
        return None
    # Sample across a long question rather than always taking only the first
    # parts: 8 parts with a limit of 3 becomes 1, 5, 8.
    indices = [round(index * (len(numbers) - 1) / (take - 1)) for index in range(take)]
    wanted = [numbers[index] for index in dict.fromkeys(indices)]
    def pick(parts: list[tuple[str, str]], preface: str) -> str:
        chosen = [value for no, value in parts if no in wanted]
        return "\n".join(([preface] if preface else []) + chosen).strip()
    return {
        "selected_subparts": wanted,
        "content": pick(stem[1], stem[0]),
        "answer": pick(answer[1], answer[0]),
        "rubric": pick(rubric[1], rubric[0]),
        "parts": [
            {
                "subpart_no": no,
                # Show the parent instruction once, before the first selected
                # sub-question (for example: “求下列函数的导数：”).
                "content": ((re.sub(r"^\s*\d+\s*[.．、]\s*", "", stem[0]).strip() + "\n") if no == wanted[0] and stem[0].strip() else "")
                           + next(value for key, value in stem[1] if key == no),
                "answer": next(value for key, value in answer[1] if key == no),
                "rubric": next(value for key, value in rubric[1] if key == no),
            }
            for no in wanted
        ],
    }


def _assignment_quality_reason(row: dict[str, Any]) -> str | None:
    """Reject visibly broken imports before they can reach a student worksheet."""
    stem = str(row.get("content") or "").strip()
    answer = str(row.get("answer") or "").strip()
    plain = re.sub(r"\\[A-Za-z]+(?:\{[^{}]*\})*", "", stem)
    plain = re.sub(r"[^\w\u4e00-\u9fff]+", "", plain)
    if len(plain) < 12 or not re.search(r"求|证明|计算|判断|作图|讨论|解|导数|积分", stem):
        return "题干不完整"
    # Typical OCR corruption from this import batch: x2 rather than x^2,
    # and U/二 standing in for y= or other formula glyphs.
    if re.search(r"(?:\b[xy]2\b|U\s*二)", stem):
        return "题干含明显 OCR 乱码"
    # A new textbook question accidentally appended to an answer is never safe
    # for automatic grading, even when the visible stem happens to look valid.
    if re.search(r"\n\s*\d{1,2}[.．、]\s*(?:设|求|已知|利用)", answer):
        return "答案混入下一题"
    return None


def _select_by_tier(rows: list[dict], question_count: int,
                    basic_ratio: float, advanced_ratio: float) -> list[dict]:
    """Pick problems to match the required 基础/提高/综合 composition.

    Falls back to any remaining published problem when a tier is exhausted, so a
    section with an uneven distribution still yields a full paper.
    """
    by_level: dict[str, list[dict]] = {"基础": [], "提高": [], "综合": []}
    for row in rows:
        by_level.setdefault(row["difficulty"], []).append(row)
    for pool in by_level.values():
        random.shuffle(pool)
        # Within a tier, prefer questions students can complete in the
        # worksheet answer space; randomness remains among equal-size items.
        pool.sort(key=lambda row: _subpart_count(row.get("content") or ""))

    basic_count = round(question_count * basic_ratio)
    advanced_count = round(question_count * advanced_ratio)
    requested = (
        ["基础"] * basic_count
        + ["提高"] * advanced_count
        + ["综合"] * (question_count - basic_count - advanced_count)
    )

    selected: list[dict] = []
    used: set[int] = set()
    for level in requested:
        candidate = next((r for r in by_level.get(level, []) if r["id"] not in used), None)
        if candidate is None:
            candidate = next((r for r in rows if r["id"] not in used), None)
        if candidate is None:
            break
        selected.append(candidate)
        used.add(candidate["id"])
    return selected


def assemble(sections: list[str], *, title: str, class_id: int, class_name: str, due_at: datetime,
             question_count: int = 6, basic_ratio: float = 0.5, advanced_ratio: float = 0.35,
             subpart_limit: int = 3, build_pdf: bool = True, out_dir: str | None = None) -> dict[str, Any]:
    """Build a homework assignment from cached, stratified problems.

    Returns a structured result: assignment id, selected problems, the achieved
    3-2-1 composition, and (when requested) the rendered PDF path + page count.
    """
    if basic_ratio + advanced_ratio > 1:
        raise ValueError("基础和提高比例之和不能超过 1")
    if not sections:
        raise ValueError("至少需要一个章节")

    with connection() as conn:
        placeholders = ",".join("?" for _ in sections)
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM questions WHERE chapter IN ({placeholders}) "
                f"AND review_status='published'",
                sections,
            ).fetchall()
        ]
    if not rows:
        raise ValueError(
            "所选章节暂无可发布题目，请先执行同步与分层（sync_section / stratify）"
        )

    eligible_rows = [row for row in rows if _assignment_quality_reason(row) is None]
    if not eligible_rows:
        raise ValueError("所选章节没有通过题干质量检查的可发布题目")
    selected = _select_by_tier(eligible_rows, question_count, basic_ratio, advanced_ratio)
    if len(selected) < question_count:
        raise ValueError(
            f"请求生成 {question_count} 道题，但章节中只有 {len(selected)} 道通过质量校验的已验证题目。"
            "系统没有生成不完整草稿；请补充该章节题库或减少题目数后再试。"
        )

    # Each selected parent stays in assignment_questions.  When all three
    # evidence fields are aligned, its selected sub-parts are also persisted
    # individually for rendering and grading.
    variants = [_partial_variant(row, subpart_limit) for row in selected]
    selection_notes = []
    for row, variant in zip(selected, variants):
        if _subpart_count(str(row.get("content") or "")) >= subpart_limit and not variant:
            selection_notes.append(
                f"{_source_locator(row, 0)} 未拆小问：标准答案或评分点不能可靠逐问对应，已保留整题。"
            )
    # Every newly generated assignment follows the same transparent score
    # contract: completion 20 + answer quality 80 = 100.  The per-question
    # scores below are the 80-point quality component only.
    completion_points, quality_points, total_points = 20.0, 80.0, 100.0
    parent_scores: list[float] = []
    allocated_parent = 0.0
    for index in range(len(selected)):
        parent_score = (round(quality_points - allocated_parent, 2)
                        if index == len(selected) - 1 else round(quality_points / len(selected), 2))
        allocated_parent += parent_score
        parent_scores.append(parent_score)
    with connection() as conn:
        cursor = conn.execute(
            "INSERT INTO assignments(title,chapter,class_name,class_id,due_at,total_score,status,score_policy,completion_points) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (title, "、".join(sections), class_name, class_id, due_at.isoformat(), total_points, "draft",
             "completion20_quality80_v1", completion_points),
        )
        assignment_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO assignment_questions(assignment_id,question_id,sort_order,score,original_no) "
            "VALUES(?,?,?,?,?)",
            [(assignment_id, r["id"], i + 1, parent_scores[i], _source_locator(r, i + 1))
             for i, r in enumerate(selected)],
        )
        part_rows = []
        for parent_order, (row, variant) in enumerate(zip(selected, variants), start=1):
            if not variant:
                continue
            parent_score = parent_scores[parent_order - 1]
            part_score = round(parent_score / len(variant["parts"]), 2)
            allocated = 0.0
            for order, part in enumerate(variant["parts"], start=1):
                item_score = round(parent_score - allocated, 2) if order == len(variant["parts"]) else part_score
                allocated += item_score
                part_rows.append((assignment_id, row["id"], part["subpart_no"], order,
                                  part["content"], part["answer"], part["rubric"], item_score))
        if part_rows:
            conn.executemany(
                "INSERT INTO assignment_question_parts(assignment_id,question_id,subpart_no,part_order,content,answer,rubric,score) "
                "VALUES(?,?,?,?,?,?,?,?)", part_rows)

    pdf_path: str | None = None
    page_count: int | None = None
    if build_pdf:
        target_dir = Path(out_dir) if out_dir else Path(settings.upload_dir) / "assignments"
        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / f"assignment_{assignment_id}.pdf"
        items = []
        for idx, (row, variant) in enumerate(zip(selected, variants), start=1):
            original_no = _source_locator(row, idx)
            if variant:
                for part in variant["parts"]:
                    items.append({"content": part["content"], "question_type": row.get("question_type") or "",
                                  "original_no": f"{original_no}（{part['subpart_no']}）", "img_path": None})
            else:
                items.append({"content": row["content"], "question_type": row.get("question_type") or "",
                              "original_no": original_no, "img_path": None})
        assignment_meta = {
            "title": title,
            "chapter": "、".join(sections),
            "class_name": class_name,
            "due_at": due_at.isoformat(),
        }
        path, n = build_assignment_pdf(assignment_meta, items, out_path)
        pdf_path, page_count = path, n

    composition = {
        level: sum(1 for r in selected if r["difficulty"] == level)
        for level in ("基础", "提高", "综合")
    }
    return {
        "assignment_id": assignment_id,
        "sections": sections,
        "selected_ids": [r["id"] for r in selected],
        "composition": composition,
        "question_count": len(selected),
        "effective_item_count": sum(len(v["parts"]) if v else 1 for v in variants),
        "selection_notes": selection_notes,
        "total_score": total_points,
        "completion_points": completion_points,
        "quality_points": quality_points,
        "pdf_path": pdf_path,
        "page_count": page_count,
        "problems": [
            {
                "original_no": _source_locator(r, i + 1),
                "difficulty": r["difficulty"],
                "question_type": r.get("question_type"),
                "selected_subparts": variants[i].get("selected_subparts") if variants[i] else None,
            }
            for i, r in enumerate(selected)
        ],
    }
