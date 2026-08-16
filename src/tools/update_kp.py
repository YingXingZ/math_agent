# -*- coding: utf-8 -*-
"""更新数据库中已有题目的知识点标签为 SECTION_KP 中的细粒度标签"""
import sys, sqlite3
sys.stdout.reconfigure(encoding="utf-8")
from extract_book import SECTION_KP

DB = "api.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("SELECT DISTINCT exercise_set FROM problems ORDER BY exercise_set")
sections = [r[0] for r in cur.fetchall()]

updated = 0
for sec in sections:
    kp_list = SECTION_KP.get(sec, [f"ch{sec.split('.')[0]}.overview"])
    kp = ",".join(kp_list)
    cur.execute("UPDATE problems SET knowledge_pts=? WHERE exercise_set=?", (kp, sec))
    updated += cur.rowcount
    print(f"{sec}: {kp} -> {cur.rowcount} 条")

conn.commit()
conn.close()
print(f"\n共更新 {updated} 条记录")
