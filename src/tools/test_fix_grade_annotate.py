# -*- coding: utf-8 -*-
"""验证：自动批改修复 + 手写批注 API"""
import json, requests, sqlite3, uuid
from datetime import datetime

BASE = "http://localhost:8011/api"

def test_auto_grade():
    conn = sqlite3.connect("api.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    hw = c.execute("SELECT id, problem_ids FROM homeworks LIMIT 1").fetchone()
    assert hw, "没有作业"
    pids = json.loads(hw["problem_ids"] or "[]")
    assert pids, "作业没有题目"
    sid = str(uuid.uuid4())
    answers = [{"problem_id": pids[0], "text": "test answer"}]
    c.execute(
        "INSERT INTO submissions(id,homework_id,student_no,student_name,submitted_at,status,score,answers,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, hw["id"], "T0001", "测试学生", datetime.now().isoformat(timespec="seconds"),
         "pending", None, json.dumps(answers, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
    conn.commit(); conn.close()

    r = requests.post(f"{BASE}/grade/auto?submission_id={sid}")
    assert r.status_code == 200, f"自动批改失败: {r.text}"
    data = r.json()
    assert "total_score" in data and "results" in data
    print("✅ 自动批改 OK, score:", data["total_score"], "/", data["max_score"])
    return sid

def test_annotations(sid):
    r = requests.post(f"{BASE}/submissions/{sid}/annotations",
                      json={"problem_id": "pid-1",
                            "strokes": [{"tool": "pen", "color": "#ef4444", "width": 3,
                                         "points": [{"x": 10, "y": 10}, {"x": 50, "y": 50}]}]})
    assert r.status_code == 200, f"保存批注失败: {r.text}"
    r = requests.get(f"{BASE}/submissions/{sid}/annotations")
    assert r.status_code == 200
    data = r.json()
    assert any(a["problem_id"] == "pid-1" for a in data)
    print("✅ 手写批注保存/读取 OK")

if __name__ == "__main__":
    sid = test_auto_grade()
    test_annotations(sid)
    print("全部验证通过")
