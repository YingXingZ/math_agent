#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only inspection of the user's local workbench DB to find recognition failures."""
import sqlite3
import os

DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

tables = [r["name"] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("TABLES:", tables)

# answer_import_candidates columns
try:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(answer_import_candidates)")]
    print("\nanswer_import_candidates cols:", cols)
except Exception as e:
    print("no answer_import_candidates:", e)

# Find failing / fallback candidates
print("\n=== candidates by vision_status ===")
for row in conn.execute(
    "SELECT vision_status, COUNT(*) c FROM answer_import_candidates GROUP BY vision_status"):
    print(dict(row))

print("\n=== candidates whose latex_text looks like a fallback ===")
fallback_marks = ("标准答案字段需人工整理", "结构化结果需人工整理", "服务器已识别",
                  "未识别", "请补充对应答案图片或人工填写")
q = " OR ".join(f"latex_text LIKE '%{m}%'" for m in fallback_marks)
for row in conn.execute(f"SELECT id, section_no, problem_no, sub_no, vision_status, vision_confidence, latex_text FROM answer_import_candidates WHERE {q} ORDER BY section_no, problem_no LIMIT 60"):
    d = dict(row)
    lt = (d.get("latex_text") or "")[:80].replace("\n", "\\n")
    print(f"[{d['section_no']}-{d['problem_no']}{('/'+str(d['sub_no'])) if d['sub_no'] else ''}] status={d['vision_status']} conf={d['vision_confidence']} :: {lt}")

conn.close()
