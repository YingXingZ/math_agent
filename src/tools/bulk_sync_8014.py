"""Bulk-sync 8014 problems that already have BOTH content_text + std_answer
into the Agent (8000) questions table, mirroring ai_stem_review._upsert_local_cache
mapping.

UPSERT（默认）：按 source_problem_id 匹配，已存在则 UPDATE（推送 8014 修正后的
题干/答案），不存在则 INSERT。这样 8014 重新对齐(realign)后，Agent 端答案也会被纠正，
且不动 question id（assignments/submissions 的引用保持有效）。

加 --insert-only 退回旧行为（仅插入，跳过已存在）。
"""
import os, sys, json, sqlite3, shutil, re
from datetime import datetime
from pathlib import Path

# 损坏判别：全角拉丁字母 / 全角 ~ ' \ | / 私有区字符 的密度。
# 干净的 LaTeX OCR 几乎不含这些；mojibake 会密集出现。阈值 0.006 干净分离 干净/损坏。
FW_LATIN = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]")
FW_PUNCT = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]")
PUA = re.compile(r"[\uE000-\uF8FF]")
GARBAGE_THRESH = 0.006

def garbage_score(t):
    n = len(t)
    if n == 0:
        return 0.0
    hits = len(FW_LATIN.findall(t)) + len(FW_PUNCT.findall(t)) + len(PUA.findall(t))
    return hits / n

PROJECT = r"D:\My File\大四\高数教材答案\高数作业助手"
SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
AGENT_DB = os.path.join(PROJECT, "data", "homework.db")
os.environ["DATABASE_PATH"] = AGENT_DB
sys.path.insert(0, PROJECT)

from app.db import connection, normalize_question_type, tag_knowledge_points

def diff_label(v):
    try:
        n = int(v)
        return "基础" if n <= 1 else "提高" if n == 2 else "综合"
    except (TypeError, ValueError):
        t = str(v or "")
        return t if t in {"基础", "提高", "综合"} else "提高"

def main():
    insert_only = "--insert-only" in sys.argv
    src = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        """SELECT p.id, p.problem_no, p.sub_no, p.ptype, p.difficulty, p.content_text,
                  p.std_answer, p.grading_steps, p.full_solution, p.crop_image_path, p.answer_status, s.section_no
           FROM problems p JOIN sections s ON p.section_id=s.id
           WHERE p.content_text IS NOT NULL AND p.content_text<>''
             AND p.std_answer IS NOT NULL AND p.std_answer<>''"""
    ).fetchall()
    print(f"[src] 题干+答案齐全的 problems: {len(rows)}  模式: {'insert-only' if insert_only else 'upsert'}")

    inserted = updated = skipped = blocked = 0
    with connection() as conn:
        existing = {r[0] for r in conn.execute("SELECT source_problem_id FROM questions WHERE source_problem_id IS NOT NULL").fetchall()}
        for r in rows:
            sid = str(r["id"])
            content = (r["content_text"] or "").strip()
            answer = (r["std_answer"] or "").strip()
            # 损坏门槛：过短 / 含替换符 / 全角乱码密度超阈值 → 阻断，不放给学生
            if len(content) < 6 or "�" in content or garbage_score(content) > GARBAGE_THRESH:
                status = "blocked"
            else:
                status = "published"
            qtype = normalize_question_type(r["ptype"])
            difficulty = diff_label(r["difficulty"])
            section_no = r["section_no"]
            rubric = (r["grading_steps"] or r["full_solution"] or "").strip()
            kp = tag_knowledge_points(content, answer, section_no)
            evidence = json.dumps(
                {"source": "8014", "section_no": section_no, "crop_image_path": r["crop_image_path"],
                 "answer_status": r["answer_status"]},
                ensure_ascii=False,
            )
            prob_no = str(r["problem_no"]) + (f".{r['sub_no']}" if r["sub_no"] else "")
            if sid in existing and not insert_only:
                conn.execute(
                    """UPDATE questions SET content=?, chapter=?, difficulty=?, question_type=?,
                       answer=?, rubric=?, source_evidence_json=?, source_problem_no=?, knowledge_points=?, review_status=?
                     WHERE source_problem_id=?""",
                    (content, section_no, difficulty, qtype, answer, rubric, evidence, prob_no, kp, status, sid),
                )
                updated += 1
            elif sid in existing:
                skipped += 1
            else:
                conn.execute(
                    """INSERT INTO questions
                       (content, chapter, difficulty, question_type, answer, rubric,
                        source_evidence_json, source_problem_id, source_problem_no, knowledge_points, review_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (content, section_no, difficulty, qtype, answer, rubric,
                     evidence, sid, prob_no, kp, status),
                )
                inserted += 1
            if status == "blocked":
                blocked += 1
        # 退役：源 8014 题已不再“题干+答案齐全”的 Agent 题（避免陈旧/损坏数据继续服务学生）
        complete_ids = {str(r["id"]) for r in rows}
        retired = 0
        for (sid,) in conn.execute("SELECT source_problem_id FROM questions WHERE source_problem_id IS NOT NULL"):
            if sid not in complete_ids:
                conn.execute("UPDATE questions SET review_status='blocked', answer='' WHERE source_problem_id=?", (sid,))
                retired += 1
    print(f"[done] inserted={inserted} updated(upsert)={updated} skipped(insert-only)={skipped} blocked(题干损坏)={blocked} retired(源不再齐全)={retired}")
    # 终态
    a = sqlite3.connect(AGENT_DB)
    print("[agent] questions 总数:", a.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
    print("[agent] review_status:", dict(a.execute("SELECT review_status, COUNT(*) FROM questions GROUP BY review_status").fetchall()))

if __name__ == "__main__":
    main()
