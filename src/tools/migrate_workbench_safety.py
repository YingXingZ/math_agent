"""One-time safety migration for the existing workbench database.

It never deletes questions or submissions.  It makes legacy OCR-derived answers
non-gradeable until a teacher verifies them, removes duplicate homework items,
and recalculates each homework's 100-point allocation.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def normalize(problem_ids: list[str]) -> tuple[list[str], dict[str, float]]:
    ids = list(dict.fromkeys(str(pid) for pid in problem_ids if pid))
    if not ids:
        return [], {}
    base, rest = divmod(100, len(ids))
    return ids, {pid: float(base + (1 if i < rest else 0)) for i, pid in enumerate(ids)}


def main(path: str) -> None:
    db = Path(path)
    if not db.is_file():
        raise SystemExit(f"database not found: {db}")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE problems SET answer_status='unverified', answer_invalid_reason='需要教师核验后才可自动评分'")
        changed = 0
        for row in conn.execute("SELECT id, problem_ids FROM homeworks").fetchall():
            ids, points = normalize(json.loads(row[1] or "[]"))
            conn.execute("UPDATE homeworks SET problem_ids=?, points_map=? WHERE id=?", (json.dumps(ids), json.dumps(points), row[0]))
            changed += 1
        conn.execute("UPDATE submissions SET status='review_required' WHERE status='graded'")
        conn.commit()
    print(f"Safety migration complete: {changed} homework(s) normalized; all legacy answers require verification.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "api.db")
