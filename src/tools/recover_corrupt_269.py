# -*- coding: utf-8 -*-
"""对 §2.6/#2.7/#2.9 等 CORRUPT 行，用答案书 OCR 文本做全角->半角规整后回填 8014。
策略：
  - 缺失 stem -> 补 OCR stem（规整后过 garbage_score 且 len>=4）
  - 缺失 answer -> 补 OCR answer
  - 已有 answer 是【桩答案】(len<30 且含 ；/) 或【损坏】(规整后仍 fail garbage_score)
        -> 用干净 OCR answer 覆盖（提升质量，避免发布桩/损坏答案）
  - 真实长答案（规整后过门禁）保留不动
  - 答案书父题干只有 "N." 的行（§2.6#7/#8）stem 补不出 -> 仍 incomplete，继续 blocked
先 dry-run（无 --apply）打印每行的补/覆盖决策与预览，确认后再写库。
复用 recover_from_answer_ocr.build_book_index() 保证匹配口径一致。
"""
import os, sys, re, sqlite3
sys.path.insert(0, r"D:\workbuddy\2026-08-06-15-31-48")
import recover_from_answer_ocr as R

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
TARGET = ["2.6", "2.7", "2.9"]

def normalize_fullwidth(t):
    if not t:
        return t
    out = []
    for ch in t:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)

def fw_count(t):
    g = 0
    for ch in t:
        o = ord(ch)
        if 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            g += 1
        elif o in (0xFF5E, 0xFF07, 0xFF3C, 0xFF5C):
            g += 1
        elif 0xE000 <= o <= 0xF8FF:
            g += 1
    return g

def score(t):
    n = len(t)
    return (fw_count(t) / n) if n else 0.0

def is_stub(a):
    a = a.strip()
    if len(a) < 30 and ("；" in a or ";" in a):
        return True
    return False

def sub_key(sub_no):
    if not sub_no:
        return None
    m = re.search(r"\((\d{1,2})\)", sub_no)
    return m.group(1) if m else None

def main():
    apply = "--apply" in sys.argv
    idx = R.build_book_index()
    s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
    rows = s.execute("""SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.std_answer,
                             p.answer_status, s.section_no
                      FROM problems p JOIN sections s ON s.id=p.section_id
                      ORDER BY s.section_no, p.problem_no, p.sub_no""").fetchall()
    plan = []
    for r in rows:
        sec = r["section_no"]
        if sec not in TARGET:
            continue
        no = str(r["problem_no"]); sub = r["sub_no"]; sk = sub_key(sub)
        has_c = bool(r["content_text"] and r["content_text"].strip())
        has_a = bool(r["std_answer"] and r["std_answer"].strip())
        entry = idx.get(sec, {}).get(no)
        if entry is None:
            print(f"  [SKIP] §{sec}#{no}{sub or ''} 答案书无此题号")
            continue
        rel_stem = entry["stem_subs"].get(sk) if (sk and sk in entry["stem_subs"]) else entry["stem"]
        rel_ans = entry["subs"].get(sk) if (sk and sk in entry["subs"]) else entry["solution"]
        rec = {"sec": sec, "no": no, "sub": sub or "", "pid": r["id"],
               "status": r["answer_status"], "ops": []}
        # 1) stem
        if not has_c and rel_stem:
            ns = normalize_fullwidth(rel_stem)
            if len(ns.strip()) >= 4 and score(ns) <= R.GARBAGE_THRESH:
                rec["ops"].append(("stem", ns))
        elif has_c:
            # 既有 stem 含全角标点 -> 规整（仅当其 fail 门禁时），让同步能过垃圾门禁
            cur = normalize_fullwidth(r["content_text"])
            if cur != r["content_text"] and score(cur) <= R.GARBAGE_THRESH and len(cur.strip()) >= 4:
                rec["ops"].append(("stem_normalize", cur))
        # 2) answer: 缺失 或 桩/损坏 -> 用干净 OCR 覆盖
        ans_from_ocr = None
        if rel_ans:
            na = normalize_fullwidth(rel_ans)
            if len(na.strip()) >= 2 and score(na) <= R.GARBAGE_THRESH:
                ans_from_ocr = na
        if not has_a and ans_from_ocr:
            rec["ops"].append(("answer", ans_from_ocr))
        elif has_a:
            cur = normalize_fullwidth(r["std_answer"])
            if (is_stub(r["std_answer"]) or score(cur) > R.GARBAGE_THRESH) and ans_from_ocr:
                rec["ops"].append(("answer_overwrite", ans_from_ocr))
            elif cur != r["std_answer"] and score(cur) <= R.GARBAGE_THRESH and len(cur.strip()) >= 2:
                # 既有 answer 含全角标点 -> 规整
                rec["ops"].append(("answer_normalize", cur))
        if rec["ops"]:
            plan.append(rec)
    # 报告
    print(f"=== §2.6/#2.7/#2.9 可规整回填/覆盖: {len(plan)} 行 (apply={apply}) ===")
    for rec in plan:
        print(f"\n§{rec['sec']}#{rec['no']}{rec['sub']} 原status={rec['status']}")
        for kind, txt in rec["ops"]:
            print(f"   [{kind}] score={score(txt):.4f} len={len(txt)}")
            print("    " + txt[:110].replace("\n", "\\n"))
    if not apply:
        print("\n[DRY-RUN] 未写库。确认预览无误后加 --apply。")
        return
    # 写库
    nw = 0
    for rec in plan:
        sets, params = [], []
        for kind, txt in rec["ops"]:
            if kind == "stem" or kind == "stem_normalize":
                sets.append("content_text=?"); params.append(txt)
            else:  # answer / answer_overwrite / answer_normalize
                sets.append("std_answer=?"); params.append(txt)
                sets.append("full_solution=?"); params.append(txt)
                if kind != "answer_normalize":
                    sets.append("answer_status='recovered'")
        if not sets:
            continue
        params.append(rec["pid"])
        s.execute("UPDATE problems SET " + ", ".join(sets) + " WHERE id=?", params)
        nw += 1
    s.commit(); s.close()
    print(f"\n[APPLIED] 写回 {nw} 行到 8014。")

if __name__ == "__main__":
    main()
