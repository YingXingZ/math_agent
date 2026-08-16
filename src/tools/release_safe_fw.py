# -*- coding: utf-8 -*-
"""定向放出已人工核验干净的全角规整题（绝不放出含中文乱码/公式误读不可推断的题）。

白名单 = S6.2#4, S6.7#8：答案干净 LaTeX，题干仅公式符号误读（√→、/ , ∂→Z）可推断，
不含中文 OCR 乱码。只规整这两题并写回 8014，随后由 bulk_sync 放出。其余 39 道
（含中文乱码 / 缺答案）一律不动，转 VLM 重识别。

安全复检：写库前对规整结果再算 dirty()（\ufffd 或 garbage_score>thr），任一字段仍 dirty 则跳过。
"""
import sys, re, sqlite3

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
GARBAGE_THRESH = 0.006
FIELDS = ["content_text", "std_answer", "full_solution"]
WHITELIST = [("6.2", "4", ""), ("6.7", "8", "")]

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

FW_LATIN = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]")
FW_PUNCT = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]")
PUA = re.compile(r"[\uE000-\uF8FF]")

def garbage_score(t):
    n = len(t)
    if n == 0:
        return 0.0
    return (len(FW_LATIN.findall(t)) + len(FW_PUNCT.findall(t)) + len(PUA.findall(t))) / n

def dirty(t):
    return ("\ufffd" in t) or garbage_score(t) > GARBAGE_THRESH

def main():
    apply = "--apply" in sys.argv
    s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
    rows = s.execute(
        """SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.std_answer, p.full_solution, s.section_no
           FROM problems p JOIN sections s ON p.section_id=s.id"""
    ).fetchall()
    sel = {}
    for r in rows:
        key = (str(r["section_no"]), str(r["problem_no"]), r["sub_no"] or "")
        if key in WHITELIST:
            sel[key] = r

    print(f"=== 定向放出（白名单 {len(WHITELIST)} 题，apply={apply}）===")
    for key in WHITELIST:
        r = sel.get(key)
        if not r:
            print(f"  [MISS] {key} 在 8014 未找到"); continue
        changed = {}
        unsafe = False
        for f in FIELDS:
            old = r[f]
            if not old:
                continue
            nv = normalize_fullwidth(old)
            if nv != old:
                changed[f] = nv
                if dirty(nv):
                    unsafe = True
        if unsafe:
            print(f"  [SKIP-UNSAFE] S{key[0]}#{key[1]}{key[2]} 规整后仍含损坏，跳过（不应发生）")
            continue
        if not changed:
            print(f"  [NOCHANGE] S{key[0]}#{key[1]}{key[2]} 本无全角垃圾，无需规整")
            continue
        print(f"  [OK] S{key[0]}#{key[1]}{key[2]} 规整字段={list(changed)} 安全")
        for f, nv in changed.items():
            print(f"      {f} after: {nv[:90].replace(chr(10),' ')}")
        if apply:
            sets, params = [], []
            for f, nv in changed.items():
                sets.append(f"{f}=?"); params.append(nv)
            sets.append("answer_status='recovered'")
            params.append(r["id"])
            s.execute("UPDATE problems SET " + ", ".join(sets) + " WHERE id=?", params)
            print(f"      -> 已写回 id={r['id']}")
    if apply:
        s.commit()
        print("\n[APPLIED] 已写回白名单题。下一步运行 bulk_sync_8014.py 放出到 Agent。")
    else:
        print("\n[DRY-RUN] 未写库。确认安全后加 --apply。")

if __name__ == "__main__":
    main()
