# -*- coding: utf-8 -*-
import sys, sqlite3
sys.stdout.reconfigure(encoding="utf-8")
from grading_engine import grade_calc, ProblemSpec

conn = sqlite3.connect("api.db"); conn.row_factory = sqlite3.Row

# 导数题 c120c7b9 标准答案 3f'(x_0)
r = conn.execute("SELECT p.id,p.std_answer FROM problems p WHERE p.id LIKE ?", ("c120c7b9%",)).fetchone()
std = r["std_answer"]
print("导数标准答案:", std)
spec = ProblemSpec(pid=r["id"], ptype="calc", max_score=10, std_answer=std, answer_tol=1e-6, answer_weight=0.6)
cases = [("3f'(x0)", "正确-同形"), ("3*f'(x0)", "正确-带*"), ("5f'(x0)", "错误"), ("3*f'(x^2)", "错误-不同")]
for stu, note in cases:
    res = grade_calc(spec, stu)
    print(f"  学生[{note}] {stu!r:14} -> 正确={res.correct} 分={res.score} 置信={res.confidence} 复核={res.need_review} ({res.detail.get('method')})")

# 区间题 d61905da 标准答案 [-1,+infty)
r2 = conn.execute("SELECT p.id,p.std_answer FROM problems p WHERE p.id LIKE ?", ("d61905da%",)).fetchone()
print("\n区间标准答案:", r2["std_answer"])
spec2 = ProblemSpec(pid=r2["id"], ptype="calc", max_score=10, std_answer=r2["std_answer"], answer_tol=1e-6, answer_weight=0.6)
cases2 = [("[-1,+infty)", "正确"), ("[-1,+oo)", "正确-oo"), ("(-1,+infty)", "错误")]
for stu, note in cases2:
    res = grade_calc(spec2, stu)
    print(f"  学生[{note}] {stu!r:14} -> 正确={res.correct} 分={res.score} 置信={res.confidence} 复核={res.need_review} ({res.detail.get('method')})")
