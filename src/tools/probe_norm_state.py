# -*- coding: utf-8 -*-
"""Baseline probe for the full-width normalization rollout.
Reports current 8014 / Agent state and the exact FW-corrupt population,
plus how many become clean after normalization. Pure SQL, no app import.
"""
import re, sqlite3

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
AGENT_DB = r"D:\My File\大四\高数教材答案\高数作业助手\data\homework.db"
GARBAGE_THRESH = 0.006

FW_LATIN = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]")
FW_PUNCT = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]")
PUA = re.compile(r"[\uE000-\uF8FF]")

def garbage_score(t):
    n = len(t)
    if n == 0:
        return 0.0
    return (len(FW_LATIN.findall(t)) + len(FW_PUNCT.findall(t)) + len(PUA.findall(t))) / n

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

def dirty(t):
    return ("\ufffd" in t) or garbage_score(t) > GARBAGE_THRESH

# ---- 8014 baseline ----
s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
total = s.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
by_status = dict(s.execute("SELECT answer_status, COUNT(*) FROM problems GROUP BY answer_status").fetchall())
rows = s.execute("""SELECT p.id,p.problem_no,p.sub_no,p.content_text,p.std_answer,p.full_solution,s.section_no
                    FROM problems p JOIN sections s ON p.section_id=s.id""").fetchall()
fw_content = fw_answer = fw_any = repl_any = 0
residual_after = []   # problems still dirty after normalization
clean_publishable = []  # problems that become fully clean & publishable
for r in rows:
    c = r["content_text"] or ""; a = r["std_answer"] or ""; sol = r["full_solution"] or ""
    cg = garbage_score(c); ag = garbage_score(a); sg = garbage_score(sol)
    if cg > GARBAGE_THRESH: fw_content += 1
    if ag > GARBAGE_THRESH: fw_answer += 1
    if cg > GARBAGE_THRESH or ag > GARBAGE_THRESH or sg > GARBAGE_THRESH: fw_any += 1
    if ("\ufffd" in c) or ("\ufffd" in a): repl_any += 1
    # project after normalization
    nc = normalize_fullwidth(c); na = normalize_fullwidth(a); nsol = normalize_fullwidth(sol)
    if (cg > GARBAGE_THRESH) or (ag > GARBAGE_THRESH) or (sg > GARBAGE_THRESH):
        if dirty(nc) or dirty(na):
            residual_after.append((r["section_no"], r["problem_no"], r["sub_no"],
                                    garbage_score(nc), garbage_score(na),
                                    ("\ufffd" in nc) or ("\ufffd" in na)))
        else:
            ok = (len(nc.strip()) >= 6) and (na.strip() != "")
            clean_publishable.append((r["section_no"], r["problem_no"], r["sub_no"], ok))

print("=== 8014 baseline ===")
print(f"problems total: {total}")
print(f"by answer_status: {by_status}")
print(f"[FW] content>thr: {fw_content}   answer>thr: {fw_answer}   any-field>thr: {fw_any}")
print(f"replacement-char(ufffd) in content/answer: {repl_any}")
print(f"FW problems that become CLEAN after norm: {len(clean_publishable)}")
print(f"  of which content len>=6 AND answer non-empty (publishable): {sum(1 for x in clean_publishable if x[3])}")
print(f"FW problems STILL dirty after norm (need VLM): {len(residual_after)}")
for sec, no, sub, cg, ag, repl in residual_after:
    print(f"   RESIDUAL S{sec}#{no}{sub or ''} content_g={cg:.4f} ans_g={ag:.4f} repl={repl}")

print("\n=== Detail of all FW problems (which field, publishable after norm) ===")
for r in rows:
    c = r["content_text"] or ""; a = r["std_answer"] or ""; sol = r["full_solution"] or ""
    cg = garbage_score(c); ag = garbage_score(a); sg = garbage_score(sol)
    if not (cg > GARBAGE_THRESH or ag > GARBAGE_THRESH or sg > GARBAGE_THRESH):
        continue
    nc = normalize_fullwidth(c); na = normalize_fullwidth(a)
    fwfields = [f for f, g in (("content", cg), ("answer", ag), ("solution", sg)) if g > GARBAGE_THRESH]
    publ = (len(nc.strip()) >= 6) and (na.strip() != "") and not dirty(nc) and not dirty(na)
    print(f"  S{r['section_no']}#{r['problem_no']}{r['sub_no'] or ''} FWfields={fwfields} "
          f"clen={len(c.strip())} hasAns={bool(a.strip())} -> publishable={publ}")

# ---- Agent baseline ----
a = sqlite3.connect(AGENT_DB); a.row_factory = sqlite3.Row
print("\n=== Agent baseline ===")
print("questions total:", a.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
print("review_status:", dict(a.execute("SELECT review_status, COUNT(*) FROM questions GROUP BY review_status").fetchall()))
# published answers/content that are dirty RIGHT NOW
pub_dirty = 0
for r in a.execute("SELECT content, answer FROM questions WHERE review_status='published'"):
    if dirty(r["content"] or "") or dirty(r["answer"] or ""):
        pub_dirty += 1
print(f"currently-published questions with dirty content/answer: {pub_dirty}")
