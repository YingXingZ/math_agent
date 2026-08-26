"""Portable regression checks for the Agent report endpoints.

The test creates an empty Agent database in a temporary directory, so it never
mutates the working database or depends on an untracked binary fixture.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPOSITORY_ROOT / "src" / "agent8000"
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="agent-report-validation-"))
TEMP_DB = TEMP_ROOT / "homework.db"

os.environ["DATABASE_PATH"] = str(TEMP_DB)
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient
import app.main as main
from app.db import init_db, normalize_question_type

try:
    init_db()
    client = TestClient(main.app)
    tasks = [task["id"] for task in client.get("/api/agent/capabilities").json()["tasks"]]
    assert "reports" in tasks
    print("OK capabilities includes reports")

    quota = client.get("/api/reports/review-quota").json()
    assert {"quota_per_class", "classes", "classes_below_quota"} <= quota.keys()
    assert {"weak_points", "threshold"} <= client.get("/api/reports/weak-points").json().keys()
    assert {"students", "distribution", "class_average"} <= client.get("/api/reports/semester-summary").json().keys()
    print("OK report endpoint contracts")

    chapter = "ZZ_WEAKTEST"
    con = sqlite3.connect(TEMP_DB)
    valid_difficulty = con.execute("SELECT difficulty FROM questions LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO classes(name,semester) VALUES(?,?)", ("test class", "2026-fall"))
    class_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute(
        "INSERT INTO questions(content,chapter,difficulty,question_type,answer,rubric,source_evidence_json) VALUES(?,?,?,?,?,?,?)",
        ("test weak point", chapter, valid_difficulty, "calc", "1", "one point", "{}"),
    )
    question_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute(
        "INSERT INTO assignments(title,chapter,class_name,class_id,due_at,total_score,status,semester) VALUES(?,?,?,?,?,?,?,?)",
        ("test assignment", chapter, "test class", class_id, "2026-12-31T00:00:00Z", 100, "published", "2026-fall"),
    )
    assignment_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute(
        "INSERT INTO submissions(assignment_id,student_no,student_name,file_path,status,score,needs_review,handwriting_score) VALUES(?,?,?,?,?,?,?,?)",
        (assignment_id, "2026001", "test student", "x.pdf", "graded", 30, 0, 80),
    )
    submission_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    evidence = {"results": [{"question_id": question_id, "score": 1.0, "max_score": 10.0}]}
    con.execute(
        "INSERT INTO grading_experiences(submission_id,assignment_id,confirmed_score,teacher_feedback,evidence_json) VALUES(?,?,?,?,?)",
        (submission_id, assignment_id, 30.0, "ok", json.dumps(evidence)),
    )
    con.commit()
    con.close()

    weak_points = client.get("/api/reports/weak-points").json()["weak_points"]
    assert any(item["knowledge_point"] == chapter and item["avg_rate"] < 0.7 for item in weak_points)
    summary = client.get("/api/reports/semester-summary?class_name=test%20class&semester=2026-fall").json()
    assert len(summary["students"]) == 1 and summary["students"][0]["handwriting_avg"] == 80
    assert any(item["class_name"] == "test class" and not item["meets_quota"] for item in client.get("/api/reports/review-quota").json()["classes"])
    assert normalize_question_type("calc") == "calc" and normalize_question_type(None) == "calc"
    print("ALL VALIDATIONS PASSED")
finally:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
