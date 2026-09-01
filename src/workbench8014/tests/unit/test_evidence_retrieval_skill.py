from pathlib import Path
import os
import sqlite3
import sys

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from skills.evidence_retrieval import evidence_retrieval
from skills.schemas import EvidenceRetrievalInput


def make_db(path, status="verified", reason="", content="求极限 lim x->0 sin(x)/x"):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE sections(id TEXT PRIMARY KEY, section_no TEXT);
    CREATE TABLE problems(id TEXT PRIMARY KEY, section_id TEXT, problem_no TEXT,
      knowledge_pts TEXT, crop_image_path TEXT, full_solution TEXT,
      answer_status TEXT, answer_invalid_reason TEXT, content_text TEXT);
    INSERT INTO sections VALUES ('s1','1.1');
    """)
    conn.execute("INSERT INTO problems VALUES (?,?,?,?,?,?,?,?,?)",
        ("p1","s1","1","极限,重要极限","crop.png","private solution",status,reason,content))
    conn.commit(); conn.close()


def test_only_verified_problem_returns_safe_evidence(tmp_path, monkeypatch):
    db = tmp_path / "library.db"; make_db(db); monkeypatch.setenv("WORKBENCH_DB", str(db))
    result = evidence_retrieval(EvidenceRetrievalInput(problem_id="p1"))
    assert result.success is True
    assert result.record.section_no == "1.1"
    assert "private solution" not in str(result.model_dump())


def test_unverified_problem_is_not_teaching_evidence(tmp_path, monkeypatch):
    db = tmp_path / "library.db"; make_db(db, status="recovered"); monkeypatch.setenv("WORKBENCH_DB", str(db))
    result = evidence_retrieval(EvidenceRetrievalInput(problem_id="p1"))
    assert result.success is False
    assert result.error_code == "PROBLEM_NOT_VERIFIED"
