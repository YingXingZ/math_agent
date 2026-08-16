"""Write the 12 VERIFIED-clean IMA recoveries into 8014 problems, overwriting the
garbled content_text / std_answer / full_solution. Only pairs in CLEAN_OK are touched
(manually verified semantically correct — garbage_score alone is insufficient because it
misses Chinese-character misreads).
"""
import csv, sqlite3, os

DB = r"D:/My File/大四/高数教材答案/api.workbench.db"
CSV = r"D:/workbuddy/2026-08-06-15-31-48/extract_plan_39.csv"

# (section, problem) manually verified clean & correct
CLEAN_OK = [
    ("1.1", 9), ("1.2", 6), ("1.5", 8), ("2.1", 14), ("2.2", 4), ("2.2", 7),
    ("6.2", 9), ("6.3", 1), ("6.3", 4), ("6.5", 4), ("6.5", 5), ("6.6", 2),
]

rows = { (r["section"], int(r["problem"])): r for r in csv.DictReader(open(CSV, encoding="utf-8-sig")) }

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
cur = con.cursor()
written = 0
for es, n in CLEAN_OK:
    r = rows.get((es, n))
    if not r:
        print(f"CSV missing {es}#{n}"); continue
    content = r["content_text"].strip()
    answer = r["std_answer"].strip()
    full = r["full_solution"].strip()
    if not content or not answer:
        print(f"SKIP {es}#{n}: empty content/answer"); continue
    cur.execute("SELECT id, content_text, std_answer, answer_status FROM problems WHERE exercise_set=? AND problem_no=? AND (sub_no IS NULL OR sub_no='')", (es, str(n)))
    hit = cur.fetchone()
    if not hit:
        print(f"DB missing {es}#{n}"); continue
    print(f"WRITE {es}#{n} id={hit['id']}  old_status={hit['answer_status']}  clen={len(content)} alen={len(answer)}")
    cur.execute("UPDATE problems SET content_text=?, std_answer=?, full_solution=?, answer_status='recovered' WHERE id=?",
                (content, answer, full, hit["id"]))
    written += 1
con.commit(); con.close()
print(f"\nWrote {written} clean recoveries to 8014.")
