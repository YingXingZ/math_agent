"""Portable regression checks for student submission hardening."""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPOSITORY_ROOT / "src" / "agent8000"
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="student-submit-regression-"))
TEMP_DB = TEMP_ROOT / "homework.db"
UPLOAD_DIR = TEMP_ROOT / "uploads"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"

os.environ["DATABASE_PATH"] = str(TEMP_DB)
sys.path.insert(0, str(PROJECT))

import app.main as main
from app.db import init_db
from fastapi.testclient import TestClient

try:
    init_db()
    main.settings.upload_dir = str(UPLOAD_DIR)
    client = TestClient(main.app)
    with patch.object(main, "run_grading_job", lambda _job_id: None):
        con = sqlite3.connect(TEMP_DB)
        con.execute("INSERT INTO classes(name,semester) VALUES(?,?)", ("test class", "2026-fall"))
        class_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO students(class_id,student_no,name) VALUES(?,?,?)", (class_id, "TESTSUB009", "test"))
        con.execute(
            "INSERT INTO assignments(title,chapter,class_name,class_id,due_at,total_score,status,semester) VALUES(?,?,?,?,?,?,?,?)",
            ("submission regression", "ZZ_TEST", "test class", class_id, "2026-12-31T00:00:00Z", 100, "published", "2026-fall"),
        )
        assignment_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()
        con.close()
        def submit(student_no: str, filename: str, payload: bytes = PDF):
            return client.post(
                f"/api/assignments/{assignment_id}/submissions",
                data={"student_no": student_no, "student_name": "test"},
                files={"file": (filename, payload, "application/pdf")},
            )

        assert submit("a/../b", "hw.pdf").status_code == 422
        assert submit("TESTSUB002", "", b"x").status_code == 422
        assert submit("TESTSUB003", "x.txt", b"hello").status_code == 415
        accepted = submit("TESTSUB009", "hw.pdf")
        assert accepted.status_code == 201 and "grading_job_id" in accepted.json()
        submission_id = accepted.json()["id"]
        con = sqlite3.connect(TEMP_DB)
        status, file_path = con.execute("SELECT status,file_path FROM submissions WHERE id=?", (submission_id,)).fetchone()
        con.close()
        assert status == "submitted" and Path(file_path).is_absolute() and Path(file_path).is_file()
        assert submit("TESTSUB009", "hw2.pdf").status_code == 409
    print("ALL SUBMIT VALIDATIONS PASSED")
finally:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
