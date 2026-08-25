from __future__ import annotations

import sqlite3
from pathlib import Path

from src.agent8000.app.pdf_evidence_pipeline import build_evidence_manifest


def test_evidence_manifest_only_stages_existing_crops(tmp_path: Path) -> None:
    agent_db, workbench_db, image_root = tmp_path / "agent.db", tmp_path / "workbench.db", tmp_path / "images"
    image_root.mkdir(); (image_root / "crop.png").write_bytes(b"image")
    conn = sqlite3.connect(agent_db)
    conn.executescript("""CREATE TABLE questions (id INTEGER, content TEXT, answer TEXT, rubric TEXT, chapter TEXT, review_status TEXT, source_problem_id TEXT);
    INSERT INTO questions VALUES (1, '锟锟锟', 'a', '', '1.1', 'blocked', 'p1'); INSERT INTO questions VALUES (2, '锟锟锟', 'a', '', '1.1', 'blocked', 'p2'); INSERT INTO questions VALUES (3, '锟锟锟', 'a', '', '1.1', 'blocked', NULL);""")
    conn.close(); conn = sqlite3.connect(workbench_db)
    conn.executescript("""CREATE TABLE textbooks (id TEXT, name TEXT, pdf_path TEXT); CREATE TABLE sections (id TEXT, textbook_id TEXT, section_no TEXT); CREATE TABLE problems (id TEXT, section_id TEXT, problem_no TEXT, sub_no TEXT, ptype TEXT, difficulty INTEGER, crop_image_path TEXT, source_page INTEGER);
    INSERT INTO textbooks VALUES ('t', 'book', 'missing.pdf'); INSERT INTO sections VALUES ('s', 't', '1.1'); INSERT INTO problems VALUES ('p1','s','1','','calc',1,'crop.png',4); INSERT INTO problems VALUES ('p2','s','2','','calc',1,'',4);""")
    conn.close()
    manifest = build_evidence_manifest(agent_db, workbench_db, image_root, tmp_path)
    assert manifest["summary"]["ready_for_teacher_review"] == 1
    assert manifest["summary"]["needs_source_evidence"] == 1
    assert manifest["summary"]["needs_source_binding"] == 1
    assert manifest["candidates"][0]["problem_id"] == "p1"
