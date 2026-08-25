"""Freeze the live risk queue as RISK_CANDIDATE metadata only."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent8000.app.pdf_evidence_pipeline import live_garble_reasons  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-db", type=Path, required=True)
    parser.add_argument("--workbench-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    agent = sqlite3.connect(args.agent_db); agent.row_factory = sqlite3.Row
    source = sqlite3.connect(args.workbench_db); source.row_factory = sqlite3.Row
    try:
        origins = {str(row["id"]): dict(row) for row in source.execute(
            "SELECT p.id,p.problem_no,p.sub_no,s.section_no FROM problems p JOIN sections s ON s.id=p.section_id"
        )}
        items = []
        for row in agent.execute("SELECT id,content,answer,rubric,source_problem_id FROM questions ORDER BY id"):
            item = dict(row); level, reasons = live_garble_reasons(item)
            if not reasons:
                continue
            origin = origins.get(str(item.get("source_problem_id") or ""), {})
            stem = str(item.get("content") or "")
            items.append({"local_question_id": item["id"], "source_problem_id": item.get("source_problem_id"),
                          "section_no": origin.get("section_no"), "problem_no": origin.get("problem_no"),
                          "sub_no": origin.get("sub_no") or "", "current_stem_sha256": hashlib.sha256(stem.encode("utf-8")).hexdigest(),
                          "risk_level": level.upper(), "risk_reasons": reasons, "classification": "RISK_CANDIDATE"})
    finally:
        agent.close(); source.close()
    payload = {"schema_version": "risk-snapshot/v1", "snapshot_created_at": datetime.now(timezone.utc).isoformat(),
               "counts": {"total": len(items), "high": sum(item["risk_level"] == "HIGH" for item in items), "medium": sum(item["risk_level"] == "MEDIUM" for item in items)},
               "items": items}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
