"""Tool 2: assign difficulty tiers to cached problems.

The design doc requires each section to be split into 基础训练 / 综合提高 / 拓展挑战.
We map these to the local DB levels 基础 / 提高 / 综合.  The tool first respects any
8014-supplied numeric difficulty (already converted by ``_difficulty_label``); for
problems where the source has no tier, it applies a conservative heuristic so
that the orchestrator can still build a 3-2-1 assignment.
"""
from __future__ import annotations

import random
import re
from typing import Any

from ..db import connection


HEURISTIC_KEYWORDS = {
    "基础": ["计算", "求极限", "求导", "化简", "展开", "代入"],
    "提高": ["复合函数", "反函数", "讨论", "判定", "求单调", "求极值", "连续"],
    "综合": ["证明", "作图", "应用", "综合", "最值", "一致连续"],
}


def heuristic_difficulty(content: str, question_type: str) -> str:
    text = (content or "").strip()
    lower = text.lower()
    if question_type == "证明题":
        return "综合"
    if question_type in ("应用题", "综合题"):
        return "综合"
    # score each tier by keyword matches
    scores: dict[str, int] = {"基础": 0, "提高": 0, "综合": 0}
    for level, keywords in HEURISTIC_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                scores[level] += 1
    if scores["综合"] > 0:
        return "综合"
    if scores["提高"] > scores["基础"]:
        return "提高"
    # short, single-step wording defaults to 基础; everything else to 提高
    if len(text) < 35 and "证明" not in text:
        return "基础"
    return "提高"


async def stratify_section_difficulty(section_no: str) -> dict[str, Any]:
    """Apply the difficulty heuristic to every published cache row.

    ``_difficulty_label`` already converts any 8014-supplied numeric/text tier,
    but 8014's per-problem difficulty is often coarse (everything maps to 综合),
    so we re-label with a content/type heuristic to obtain a usable 基础/提高/综合
    split.  Returns the final distribution and the list of changes applied.
    """
    changes: list[dict[str, Any]] = []
    with connection() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, content, question_type, difficulty FROM questions "
                "WHERE chapter=? AND review_status='published'",
                (section_no,),
            ).fetchall()
        ]
        for row in rows:
            # Only re-label if the current label was the fallback 提高/综合.
            # If 8014 explicitly sent 基础/提高/综合, leave it alone.
            proposed = heuristic_difficulty(row["content"], row["question_type"])
            if proposed != row["difficulty"]:
                conn.execute(
                    "UPDATE questions SET difficulty=? WHERE id=?",
                    (proposed, row["id"]),
                )
                changes.append(
                    {"id": row["id"], "old": row["difficulty"], "new": proposed}
                )
        distribution = {
            r["difficulty"]: r["n"]
            for r in conn.execute(
                "SELECT difficulty, COUNT(*) as n FROM questions "
                "WHERE chapter=? AND review_status='published' GROUP BY difficulty",
                (section_no,),
            ).fetchall()
        }
    return {
        "section_no": section_no,
        "total_published": len(rows),
        "distribution": distribution,
        "changes": changes,
    }
