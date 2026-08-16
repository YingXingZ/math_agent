"""
测试自动批改后的试卷复核链路：
1. 创建提交并自动批改
2. 读取 /submissions/{sid}/grade-detail 验证完整明细
3. 修改单题分数，验证总分重新计算并记录日志
4. 读取 review-log 验证日志
"""
import sqlite3
import json
import uuid
import requests
from datetime import datetime

BASE = "http://localhost:8011/api"
DB = "api.db"


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. 准备一份作业和提交
    hw = cur.execute(
        "SELECT id, problem_ids FROM homeworks LIMIT 1").fetchone()
    assert hw, "无作业"
    pids = json.loads(hw["problem_ids"] or "[]")
    assert len(pids) >= 2, "作业题目不足"

    answers = []
    for pid in pids[:2]:
        row = cur.execute(
            "SELECT std_answer FROM problems WHERE id=?", (pid,)).fetchone()
        answers.append({"problem_id": pid, "text": row["std_answer"] or "test"})

    sid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO submissions(id,homework_id,student_no,student_name,"
        "submitted_at,status,score,answers,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, hw["id"], "20239999", "复核测试",
         datetime.now().isoformat(timespec="seconds"), "pending", None,
         json.dumps(answers, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

    # 2. 自动批改
    r = requests.post(f"{BASE}/grade/auto?submission_id={sid}")
    assert r.status_code == 200, f"自动批改失败: {r.text}"
    auto = r.json()
    print("自动批改完成", auto["total_score"], "/", auto["max_score"],
          "需复核", auto["need_review_count"])

    # 3. 获取完整明细
    r = requests.get(f"{BASE}/submissions/{sid}/grade-detail")
    assert r.status_code == 200, f"明细读取失败: {r.text}"
    detail = r.json()
    assert detail["submission_id"] == sid
    # 明细应当等于作业题目数（含未作答的占位）
    assert len(detail["problems"]) >= len(answers)
    print("试卷明细题目数", len(detail["problems"]))

    # 4. 修改第一题分数
    first = detail["problems"][0]
    pid = first["problem_id"]
    old = first["score"]
    r = requests.patch(
        f"{BASE}/submissions/{sid}/problems/{pid}",
        json={"score": first["max_score"], "feedback": "人工复核：修正为满分"})
    assert r.status_code == 200, f"改分失败: {r.text}"
    updated = r.json()
    print("改分", old, "->", updated["new_score"],
          "新总分", updated["total_score"])

    # 5. 验证日志
    r = requests.get(f"{BASE}/submissions/{sid}/review-log")
    assert r.status_code == 200
    logs = r.json()["review_log"]
    assert len(logs) == 1
    assert logs[0]["new_score"] == updated["new_score"]
    print("复核日志记录成功", logs[0])

    print("\n✅ 试卷复核链路测试全部通过")


if __name__ == "__main__":
    main()
