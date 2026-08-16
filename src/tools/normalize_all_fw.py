# -*- coding: utf-8 -*-
"""全章全角规整放量 (full-chapter full-width normalization rollout).

对 8014 problems 所有文本字段做 全角->半角 规整，清除 OCR 全角乱码
（全角拉丁字母 / 全角 ~ ' \ | / 私有区）。被垃圾门禁 block 的可读题（content 含 FW）
规整后过门禁且答案齐全 -> 可被 bulk_sync 放出。

安全约束（绝不向学生推乱码）：
  - 仅对“规整后 content 与 answer 均过 garbage_score 门禁且无替换符”的题放行；
  - 写库前对规整结果再做一遍 dirty() 复检，任何字段仍 dirty 则整题 SKIP，绝不写入；
  - dry-run（默认）只打印预测与改动清单 CSV，确认无误后加 --apply 才写库。

本脚本只动“含 FW 垃圾”的字段（规整后为 no-op 的字段不动），不改无关题。
"""
import sys, re, sqlite3, csv
from datetime import datetime

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
PLAN_CSV = r"D:\workbuddy\2026-08-06-15-31-48\normalize_plan.csv"
GARBAGE_THRESH = 0.006
FIELDS = ["content_text", "std_answer", "full_solution"]


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
        """SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.std_answer, p.full_solution,
                  p.answer_status, s.section_no
           FROM problems p JOIN sections s ON p.section_id=s.id
           ORDER BY s.section_no, p.problem_no, p.sub_no"""
    ).fetchall()

    plan = []
    safe_changed = skipped_unsafe = 0
    for r in rows:
        sec = r["section_no"]; no = str(r["problem_no"]); sub = r["sub_no"] or ""
        newvals = {}
        for f in FIELDS:
            old = r[f]
            if not old:
                continue
            nv = normalize_fullwidth(old)
            if nv != old:
                newvals[f] = nv
        if not newvals:
            continue  # 该字段无全角垃圾，不动
        # 安全复检：规整后任一字段仍 dirty -> 整题 SKIP，绝不写入
        unsafe = any(dirty(v) for v in newvals.values())
        if unsafe:
            skipped_unsafe += 1
            plan.append(dict(sec=sec, no=no, sub=sub, pid=r["id"], changed_fields=list(newvals),
                             publishable=False, written=False, note="SAFE-SKIP: 规整后仍含损坏"))
            continue
        # 发布安全性（content 与 answer 都需干净且非空）
        nc = newvals.get("content_text", r["content_text"] or "")
        na = newvals.get("std_answer", r["std_answer"] or "")
        publishable = (len(nc.strip()) >= 6) and (na.strip() != "") and (not dirty(nc)) and (not dirty(na))
        plan.append(dict(sec=sec, no=no, sub=sub, pid=r["id"], changed_fields=list(newvals),
                         publishable=publishable, written=False,
                         note="publishable" if publishable else "blocked(no answer)"))

    # ---- 报告 ----
    n_changed = len([p for p in plan if p["changed_fields"]])
    n_pub = len([p for p in plan if p["publishable"]])
    print(f"=== 全章全角规整 预测 (apply={apply}) ===")
    print(f"含全角垃圾的题(将规整): {n_changed}   其中可发布(publishable): {n_pub}   "
          f"安全跳过(unsafe): {skipped_unsafe}")
    print(f"{'sec':7} {'prob':5} {'fields':26} {'publish':9} note")
    for p in plan:
        print(f"S{p['sec']:<5}#{p['no']}{p['sub']:<4} {','.join(p['changed_fields']):26} "
              f"{str(p['publishable']):9} {p['note']}")

    with open(PLAN_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["sec", "no", "sub", "pid", "changed_fields", "publishable", "written", "note"])
        w.writeheader()
        for p in plan:
            w.writerow(p)
    print(f"\n[plan CSV] {PLAN_CSV}")

    if not apply:
        print("\n[DRY-RUN] 未写库。确认预览与安全复检(0 unsafe)无误后加 --apply。")
        return

    # ---- 写库 ----
    written = 0
    for p in plan:
        if p["note"].startswith("SAFE-SKIP"):
            continue
        vals = {f: normalize_fullwidth((r0 := [rr for rr in rows if rr["id"] == p["pid"]][0])[f] or "")
                for f in p["changed_fields"]} if False else None
        # 重新取原行规整值（上面循环已算，这里稳妥重算）
        src = [rr for rr in rows if rr["id"] == p["pid"]][0]
        sets, params = [], []
        for f in p["changed_fields"]:
            nv = normalize_fullwidth(src[f] or "")
            sets.append(f"{f}=?"); params.append(nv)
        if p["publishable"]:
            sets.append("answer_status='recovered'")
        params.append(p["pid"])
        s.execute("UPDATE problems SET " + ", ".join(sets) + " WHERE id=?", params)
        written += 1
    s.commit(); s.close()
    print(f"\n[APPLIED] 实际写回 {written} 行到 8014 problems。")
    print("[next] 运行 bulk_sync_8014.py 把可发布题推到 Agent(8000)。")


if __name__ == "__main__":
    main()
