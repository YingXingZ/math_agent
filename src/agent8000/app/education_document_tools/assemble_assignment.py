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
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import connection
from ..assignment_pdf import POINTS_PER_QUESTION, build_assignment_pdf


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
             build_pdf: bool = True, out_dir: str | None = None) -> dict[str, Any]:
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

    selected = _select_by_tier(rows, question_count, basic_ratio, advanced_ratio)
    if not selected:
        raise ValueError("所选章节没有足够的可发布题目")

    score = POINTS_PER_QUESTION
    with connection() as conn:
        cursor = conn.execute(
            "INSERT INTO assignments(title,chapter,class_name,class_id,due_at,total_score) "
            "VALUES(?,?,?,?,?,?)",
            (title, "、".join(sections), class_name, class_id, due_at.isoformat(), score * len(selected)),
        )
        assignment_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO assignment_questions(assignment_id,question_id,sort_order,score,original_no) "
            "VALUES(?,?,?,?,?)",
            [
                (assignment_id, r["id"], i + 1, score, str(i + 1))
                for i, r in enumerate(selected)
            ],
        )

    pdf_path: str | None = None
    page_count: int | None = None
    if build_pdf:
        target_dir = Path(out_dir) if out_dir else Path(settings.upload_dir) / "assignments"
        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / f"assignment_{assignment_id}.pdf"
        items = [
            {
                "content": r["content"],
                "question_type": r.get("question_type") or "",
                "original_no": str(idx + 1),
                "img_path": None,
            }
            for idx, r in enumerate(selected)
        ]
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
        "total_score": score * len(selected),
        "pdf_path": pdf_path,
        "page_count": page_count,
        "problems": [
            {
                "original_no": str(i + 1),
                "difficulty": r["difficulty"],
                "question_type": r.get("question_type"),
            }
            for i, r in enumerate(selected)
        ],
    }
