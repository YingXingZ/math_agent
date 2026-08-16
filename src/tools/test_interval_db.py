# -*- coding: utf-8 -*-
import sys, re
sys.stdout.reconfigure(encoding="utf-8")
import sqlite3
from grading_engine import classify_candidate, _parse_interval_piece, compare_interval, answer_match

conn = sqlite3.connect("api.db"); conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT p.id, p.std_answer FROM problems p WHERE p.std_answer LIKE ?", ("%\infty%",)).fetchall()
print("含 \\infty 的答案候选解析情况：")
for r in rows:
    ans = r["std_answer"]
    cands = [c.strip() for c in re.split(r"\s*\|\|\|\s*", ans) if c.strip()]
    for c in cands:
        ct = classify_candidate(c)
        pk = [p for piece in re.split(r"\\?cup", c) for p in [_parse_interval_piece(piece)] if p]
        flag = "OK" if pk else "FAIL"
        print(f"  [{ct:8}] {flag}  {c[:55]}")
    print()

# 直接验证 compare_interval 对真实 raw 形态
print("=== compare_interval 真实形态 ===")
eq, conf, how = compare_interval(r"(-\infty, -4] \cup [1, +\infty)", r"(-infty,-4]cup[1,+infty)")
print("标准并集 vs 归一化并集:", eq, conf, how)
eq, conf, how = compare_interval(r"[-1, +\infty)", r"[-1,+infty)")
print("[-1,+∞) 同形:", eq, conf, how)
