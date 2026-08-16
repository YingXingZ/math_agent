# -*- coding: utf-8 -*-
"""核验 FREEWIN 行的编号对齐：答案书解析是否真的把正确文本对准了 8014 的 (section, problem_no, sub_no)。
复用 recover_from_answer_ocr 的 build_book_index / is_clean / norm_sub。"""
import sqlite3, sys
sys.path.insert(0, r"D:\workbuddy\2026-08-06-15-31-48")
import recover_from_answer_ocr as R

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"

FREEWIN = [
    ("1.3", "6", "1", "21b54542-a4f5-4930-843d-42b35f308083"),
    ("1.3", "6", "4", "c3b234f0-6dde-4974-ae7e-f259889c8003"),
    ("1.3", "6", "7", "61368021-67fc-4a03-8fb4-7ad5fdf5f414"),
    ("2.1", "2", "(1)", "c120c7b9-e36e-48e3-be31-7b3f7f86305c"),
    ("2.1", "2", "(2)", "1614758f-538c-42e7-ba7c-bd220b03dfc0"),
    ("2.1", "2", "(3)", "a9712aa5-1a29-46f0-afb1-2220ee781e82"),
    ("2.10", "10", "", "d42f1ecd-72b3-4b4f-a803-1ff39bdaa842"),
    ("2.6", "7", "", "b90e14fc-d1d1-482b-be04-d3fe193479e3"),
    ("2.6", "8", "", "7c908b79-ba41-43c6-8249-787f3a27f1b8"),
    ("9.9", "2", "", "06fbfe69-8853-48ea-a0f3-fb79810b1b86"),
]

def sub_key(sub_no):
    if not sub_no:
        return None
    m = __import__("re").match(r"\(?(\d{1,2})\)?", str(sub_no).strip())
    return m.group(1) if m else str(sub_no).strip("()")

def main():
    con = sqlite3.connect(SRC_DB); con.row_factory = sqlite3.Row
    book = R.build_book_index()
    for sno, pno, sub, pid in FREEWIN:
        r = con.execute("""SELECT s.section_no, p.problem_no, p.sub_no, p.content_text,
                                   p.std_answer, p.answer_status
                            FROM problems p JOIN sections s ON s.id=p.section_id
                            WHERE p.id=?""", (pid,)).fetchone()
        print("=" * 78)
        print(f"FREEWIN  {sno} #{pno}{sub and ' '+sub}  id={pid}")
        print(f"  8014 当前: content_text={ (r['content_text'] or '')[:50]!r }  answer_status={r['answer_status']}")
        sec = book.get(sno)
        if sec is None:
            print("  [!] 答案书无此小节"); continue
        print(f"  答案书 {sno} 全部题号: {sorted(sec.keys(), key=lambda x:(len(x),x))}")
        prob = sec.get(pno)
        if prob is None:
            print(f"  [!] 答案书 {sno} 无 #{pno}"); continue
        sk = sub_key(sub)
        # 父题干
        print(f"  父题干(#{pno}): {prob['stem'][:90]!r}")
        # 子题干(若有)
        if sk and prob['stem_subs'].get(sk):
            print(f"  子题干({sk}): {prob['stem_subs'][sk][:90]!r}")
        # 答案
        if sk and prob['subs'].get(sk):
            print(f"  子答案({sk}): {prob['subs'][sk][:90]!r}")
        else:
            print(f"  父答案(#{pno}): {prob['solution'][:90]!r}")
        # 干净判定
        cand = prob['stem']
        print(f"  stem 干净? {R.is_clean(cand)}   答案 干净? {R.is_clean(prob['solution'])}")
        # 邻题预览（看 #6 是否真的是 习题1.3 内的题，还是 总习题 混入）
        for nb in (str(int(pno)-1) if pno.isdigit() else None,
                   str(int(pno)+1) if pno.isdigit() else None):
            if nb in sec:
                print(f"    邻题 #{nb}: {sec[nb]['stem'][:60]!r}")
    con.close()

if __name__ == "__main__":
    main()
