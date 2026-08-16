#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sqlite3, os
DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
ROOT = r"D:\My File\大四\高数教材答案"
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
# candidates that have at least one existing source image on disk
rows = conn.execute("""
SELECT c.id, c.section_no, c.problem_no, c.sub_no, c.subquestion_count,
       (SELECT COUNT(*) FROM candidate_source_images s WHERE s.candidate_id=c.id) AS nsrc
FROM answer_import_candidates c
""").fetchall()
have_img = [r for r in rows if r["nsrc"]>0 and os.path.isfile(os.path.join(ROOT, conn.execute("SELECT image_path FROM candidate_source_images WHERE candidate_id=? ORDER BY sort_order LIMIT 1",(r["id"],)).fetchone()[0]))]
print("total candidates:", len(rows))
print("candidates with >=1 source image on disk:", len(have_img))
from collections import Counter
print("subquestion_count distribution (those with images):", dict(Counter(r["subquestion_count"] for r in have_img)))
# section spread
print("sections present:", sorted(set(r["section_no"] for r in have_img)))
# how many multi-part
multi = [r for r in have_img if (r["subquestion_count"] or 0) >= 2]
print("multi-part (subquestion_count>=2) with images:", len(multi))
conn.close()
