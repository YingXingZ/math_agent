"""Create a read-only reconciliation report for crop-backed review candidates."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from tool_config import agent_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile readiness candidates with the teacher-review queue")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--agent-db", default=agent_db(), type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    ready = [item for item in manifest["candidates"] if item["disposition"] == "ready_for_teacher_review"]
    conn = sqlite3.connect(args.agent_db)
    conn.row_factory = sqlite3.Row
    ids = [str(item["problem_id"]) for item in ready]
    rows = conn.execute(
        "SELECT source_problem_id,status,confidence,review_note,reviewed_at FROM ai_stem_candidates "
        "WHERE source_problem_id IN (" + ",".join("?" for _ in ids) + ")", ids
    ).fetchall() if ids else []
    conn.close()
    by_source = {str(row["source_problem_id"]): dict(row) for row in rows}
    report_items = []
    for item in ready:
        queue = by_source.get(str(item["problem_id"]))
        state = queue["status"] if queue else "not_staged"
        report_items.append({**item, "queue_status": state, "queue": queue or {}})
    counts = Counter(item["queue_status"] for item in report_items)
    report = {"counts": dict(counts), "items": report_items}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
