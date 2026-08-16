"""Regression for student-submission hardening (8000 Agent).
Tests path-traversal guard, extension/size/empty checks, duplicate prevention,
and absolute-path storage — WITHOUT running real VLM grading (mocked)."""
import os, sys, shutil, sqlite3, tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT = r"D:\My File\大四\高数教材答案\高数作业助手"
SRC = os.path.join(PROJECT, "data", "homework.db")

TMPDB = r"D:\workbuddy\2026-08-06-15-31-48\__submit_tmp.db"
TMPUP = r"D:\workbuddy\2026-08-06-15-31-48\__submit_upload"
shutil.copy(SRC, TMPDB)
os.environ["DATABASE_PATH"] = TMPDB
sys.path.insert(0, PROJECT)

import app.main as m
from fastapi.testclient import TestClient

# hermetic upload dir + no real grading
m.settings.upload_dir = Path(TMPUP)
client = TestClient(m.app)
pat = patch.object(m, "run_grading_job", lambda jid: None)
pat.start()

ASSIGN = 2  # exists in copy
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"

fails = []

def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        fails.append(name)

# a) path-traversal / bad student_no
r = client.post(f"/api/assignments/{ASSIGN}/submissions",
                data={"student_no": "a/../b", "student_name": "x"},
                files={"file": ("hw.pdf", PDF, "application/pdf")})
check("bad student_no rejected (422)", r.status_code == 422)

# b) empty filename
r = client.post(f"/api/assignments/{ASSIGN}/submissions",
                data={"student_no": "TESTSUB002", "student_name": "x"},
                files={"file": ("", b"x", "application/pdf")})
check("empty filename rejected (422)", r.status_code == 422)

# c) bad extension
r = client.post(f"/api/assignments/{ASSIGN}/submissions",
                data={"student_no": "TESTSUB003", "student_name": "x"},
                files={"file": ("x.txt", b"hello", "text/plain")})
check("bad extension rejected (415)", r.status_code == 415)

# d) valid submission
r = client.post(f"/api/assignments/{ASSIGN}/submissions",
                data={"student_no": "TESTSUB009", "student_name": "测试生"},
                files={"file": ("hw.pdf", PDF, "application/pdf")})
ok_d = r.status_code == 201 and "grading_job_id" in r.json()
check("valid submission accepted (201) + returns grading_job_id", ok_d)
if ok_d:
    sid = r.json()["id"]
    con = sqlite3.connect(TMPDB)
    row = con.execute("SELECT status,file_path FROM submissions WHERE id=?", (sid,)).fetchone()
    con.close()
    abs_ok = row and os.path.isabs(row[1]) and os.path.isfile(row[1])
    check("submission status='submitted' & file_path absolute & on disk",
          row and row[0] == "submitted" and abs_ok)

# e) duplicate active submission -> 409 (job still 'queued' since grading mocked)
r2 = client.post(f"/api/assignments/{ASSIGN}/submissions",
                 data={"student_no": "TESTSUB009", "student_name": "测试生"},
                 files={"file": ("hw2.pdf", PDF, "application/pdf")})
check("duplicate active submission rejected (409)", r2.status_code == 409)

pat.stop()
print("\n" + ("ALL SUBMIT VALIDATIONS PASSED" if not fails else f"FAILED: {fails}"))
for f in (TMPDB,):
    try: os.remove(f)
    except OSError: pass
import shutil as _s
_s.rmtree(TMPUP, ignore_errors=True)
sys.exit(1 if fails else 0)
