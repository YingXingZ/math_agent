#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
导入后把某章节的题目批量标记为 answer_status='verified'，使其可进入
Agent 自动批改。仅作用于指定 section_no 且 std_answer 非空的题目。

用途：route2_pdf_extract.py --push 导入的 §5.1 默认 answer_status='unverified'
（8014 /ingest/book 的默认值），需核验后才对 Agent 可见为"可批改"。
这些题来自官方答案册且已抽检准确，故可批量核验。

安全：仅 UPDATE 指定章节、std_answer 非空的行；可重复执行（幂等）。
如需撤销，在 8014 教师端逐题取消核验即可。

用法：
  python verify_section_8014.py --section 5.1 --db "D:/My File/大四/高数教材答案/api.workbench.db" [--dry-run]
"""
import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = r"D:/My File/大四/高数教材答案/api.workbench.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"[error] DB 不存在: {db}")

    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        sec = conn.execute(
            "SELECT id FROM sections WHERE section_no=?", (args.section,)
        ).fetchone()
        if not sec:
            raise SystemExit(f"[error] 未找到章节 {args.section}")
        sid = sec["id"]

        rows = conn.execute(
            """SELECT id, problem_no, sub_no, std_answer, answer_status
               FROM problems WHERE section_id=? AND trim(std_answer)<>'' """,
            (sid,),
        ).fetchall()
        print(f"[section {args.section}] 候选（有答案）题目数: {len(rows)}")
        target = [r for r in rows if r["answer_status"] != "verified"]
        print(f"  其中尚未 verified: {len(target)}")
        for r in target[:50]:
            print(f"    - 题{r['problem_no']}{('/'+r['sub_no']) if r['sub_no'] else ''} "
                  f"当前={r['answer_status']} 答案[{len(r['std_answer'])}字]")

        if args.dry_run:
            print("[dry-run] 未执行更新")
            return

        cur = conn.execute(
            """UPDATE problems
               SET answer_status='verified', answer_invalid_reason=''
               WHERE section_id=? AND trim(std_answer)<>'' AND answer_status<>'verified'""",
            (sid,),
        )
        conn.commit()
        print(f"[done] 已核验 {cur.rowcount} 题 -> answer_status='verified'")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
