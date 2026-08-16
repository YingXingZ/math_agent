"""Validate the 8000 Agent report endpoints + question_type normalization
against a TEMP copy of homework.db (no production impact)."""
import os, sys, shutil, json, sqlite3, tempfile

SRC = r"D:\My File\大四\高数教材答案\高数作业助手\data\homework.db"
TMP = r"D:\workbuddy\2026-08-06-15-31-48\__validate_tmp.db"
shutil.copy(SRC, TMP)
os.environ["DATABASE_PATH"] = TMP
sys.path.insert(0, r"D:\My File\大四\高数教材答案\高数作业助手")

from fastapi.testclient import TestClient
import app.main as m
from app.db import normalize_question_type as nq

client = TestClient(m.app)

# 1) capabilities advertises the reports task
cap = client.get("/api/agent/capabilities").json()
ids = [t["id"] for t in cap["tasks"]]
assert "reports" in ids, ids
print("OK  capabilities includes 'reports':", ids)

# 2) report endpoints return 200 + expected keys (empty data)
rq = client.get("/api/reports/review-quota").json()
assert {"quota_per_class", "classes", "classes_below_quota"} <= rq.keys()
print("OK  /review-quota keys; quota_per_class =", rq["quota_per_class"])
assert client.get("/api/reports/weak-points").json().keys() >= {"weak_points", "threshold"}
print("OK  /weak-points keys")
ss = client.get("/api/reports/semester-summary").json()
assert {"students", "distribution", "class_average"} <= ss.keys()
print("OK  /semester-summary keys")

# 3) seed synthetic grading evidence to exercise aggregation
con = sqlite3.connect(TMP); con.row_factory = sqlite3.Row
# use a brand-new uniquely-named chapter so we control the score totally
chapter = "ZZ_WEAKTEST"
con.execute("INSERT INTO questions(content,chapter,difficulty,question_type,answer,rubric,source_evidence_json) "
            "VALUES(?,?,?,?,?,?,?)", ("测试薄弱题", chapter, "基础", "calc", "1", "1分", "{}"))
qid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
con.execute("INSERT INTO assignments(title,chapter,class_name,due_at,total_score,status,semester) "
            "VALUES(?,?,?,?,?,?,?)", ("测试作业", chapter, "测试班", "2026-12-31T00:00:00Z", 100, "published", "2026秋"))
aid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
con.execute("INSERT INTO submissions(assignment_id,student_no,student_name,file_path,status,score,needs_review,handwriting_score) "
            "VALUES(?,?,?,?,?,?,?,?)", (aid, "2026001", "张三", "x.pdf", "graded", 30, 0, 80))
sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
con.execute("INSERT INTO grading_experiences(submission_id,assignment_id,confirmed_score,teacher_feedback,evidence_json) "
            "VALUES(?,?,?,?,?)", (sid, aid, 30.0, "ok", json.dumps({"results":[{"question_id": qid, "score": 1.0, "max_score": 10.0}]}, ensure_ascii=False)))
con.commit(); con.close()

wp2 = client.get("/api/reports/weak-points").json()
print("    weak-points after seed:", [(p["knowledge_point"], p["avg_rate"], p["sample_count"]) for p in wp2["weak_points"]])
assert any(p["knowledge_point"] == chapter and p["avg_rate"] < 0.7 for p in wp2["weak_points"]), "章节应被标为薄弱"

ss2 = client.get("/api/reports/semester-summary?class_name=测试班&semester=2026秋").json()
print("    semester-summary students:", [(s["rank"], s["student_name"], s["average"], s["handwriting_avg"]) for s in ss2["students"]])
assert len(ss2["students"]) == 1 and ss2["students"][0]["handwriting_avg"] == 80

rq2 = client.get("/api/reports/review-quota").json()
below = [c for c in rq2["classes"] if c["class_name"] == "测试班"]
print("    review-quota 测试班:", below)
assert below and below[0]["meets_quota"] is False, "1 次复核未达配额 2"

# 4) normalize_question_type unit
assert nq("计算题") == "calc" and nq("证明题") == "proof" and nq("calc") == "calc"
assert nq("应用题") == "calc" and nq(None) == "calc" and nq("") == "calc"
print("OK  normalize_question_type passes")

print("\nALL VALIDATIONS PASSED")
try: os.remove(TMP)
except OSError: pass
