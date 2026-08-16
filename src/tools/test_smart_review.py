# -*- coding: utf-8 -*-
"""验证：章节多选、选题二次编辑 API、复核弹窗接口"""
import requests, json, sys

BASE = "http://localhost:8011/api"

def ok(resp, name):
    if resp.status_code != 200:
        print(f"FAIL {name}: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    print(f"OK   {name}")

# 1. 多章节智能选题
r = requests.post(
    f"{BASE}/homeworks/smart-select?section_nos=1.1,1.2&basic=1&medium=1&advanced=1")
ok(r, "multi-section smart-select")
data = r.json()
assert "section_nos" in data and len(data["section_nos"]) == 2
assert "problems" in data

# 2. 单章节兼容
r = requests.post(
    f"{BASE}/homeworks/smart-select?section_no=2.1&basic=1&medium=1&advanced=1")
ok(r, "single-section compatibility")

# 3. 创建多章节作业
classes = requests.get(f"{BASE}/classes").json()
assert classes, "need at least one class"
cls = classes[0]
payload = {
    "title": "单元测试多章节作业",
    "class_id": cls["id"],
    "section_no": "1.1,1.2",
    "section_nos": ["1.1", "1.2"],
    "deadline": "2026-08-10T00:00:00",
    "problem_ids": [p["id"] for p in data["problems"]]
}
r = requests.post(f"{BASE}/homeworks", json=payload)
ok(r, "create multi-section homework")
hw_id = r.json()["id"]

# 4. 复核学生详情
students = requests.get(f"{BASE}/students").json()
assert students, "need at least one student"
stu = students[0]
r = requests.get(f"{BASE}/review/student/{stu['id']}")
ok(r, "review student detail")
assert "student" in r.json() and "submissions" in r.json()

# 5. 记录复核
r = requests.post(f"{BASE}/review/record/{stu['id']}", json={"note": "测试复核"})
ok(r, "record review")
assert r.json()["review_count"] >= 1

# 6. 复核统计
r = requests.get(f"{BASE}/review/stats?class_id={cls['id']}")
ok(r, "review stats")
assert "students" in r.json()

# 清理
requests.delete(f"{BASE}/homeworks/{hw_id}")
print("All tests passed.")
