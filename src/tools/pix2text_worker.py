# -*- coding: utf-8 -*-
"""Background worker for one local Pix2Text answer-PDF recognition task.

It stores a review-only candidate in SQLite.  It intentionally never updates
``problems.std_answer`` or ``answer_status``.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path


def timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def target_excerpt(markdown: str, problem_no: str) -> str:
    """Prefer the requested numbered item but retain all text if it is unclear."""
    starts = list(re.finditer(rf"(?m)^\s*{re.escape(problem_no)}\s*[.．、]", markdown))
    if not starts:
        return markdown.strip()
    start = starts[0].start()
    following = re.search(r"(?m)^\s*\d{1,3}\s*[.．、]", markdown[starts[0].end():])
    end = starts[0].end() + following.start() if following else len(markdown)
    return markdown[start:end].strip()


def fail(conn: sqlite3.Connection, task, error: str) -> None:
    now = timestamp()
    conn.execute(
        "UPDATE vision_recognition_tasks SET status='failed',error_message=?,updated_at=? WHERE id=?",
        (error[:1000], now, task["id"]),
    )
    conn.execute("UPDATE answer_import_candidates SET vision_status='failed' WHERE id=?", (task["candidate_id"],))
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    task = conn.execute("""
        SELECT t.*, c.section_no, c.problem_no, c.sub_no
        FROM vision_recognition_tasks t
        JOIN answer_import_candidates c ON c.id=t.candidate_id
        WHERE t.id=?
    """, (args.task_id,)).fetchone()
    if not task:
        return 2
    try:
        image_path = Path(args.db).parent / task["input_image_path"]
        if not image_path.is_file():
            raise FileNotFoundError(f"source preview missing: {image_path}")
        from pix2text import Pix2Text

        recognizer = Pix2Text.from_config(enable_formula=True, enable_table=False, device="cpu")
        raw_markdown = str(recognizer.recognize(str(image_path), file_type="text_formula"))
        selected = target_excerpt(raw_markdown, str(task["problem_no"]))
        result = {
            "provider": "pix2text",
            "target": {
                "section": task["section_no"], "problem": task["problem_no"], "sub": task["sub_no"],
            },
            "latex_candidate": selected,
            "raw_page_markdown": raw_markdown,
            "confidence": 0.45,
            "notes": "本地 Pix2Text 整页识别候选；请结合原始答案页人工确认最终答案。",
        }
        now = timestamp()
        conn.execute("""
            UPDATE vision_recognition_tasks
            SET status='completed',result_json=?,error_message='',updated_at=? WHERE id=?
        """, (json.dumps(result, ensure_ascii=False), now, task["id"]))
        conn.execute("""
            UPDATE answer_import_candidates
            SET latex_text=?,vision_status='completed',vision_confidence=? WHERE id=?
        """, (selected, 0.45, task["candidate_id"]))
        conn.commit()
        return 0
    except Exception:
        fail(conn, task, traceback.format_exc())
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
