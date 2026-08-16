"""Recover clean content_text + std_answer for the 39 corrupted 8014 problems
from the IMA answer-book OCR text (which restates each problem then solves it).
Answer book is the single clean source -> avoids textbook boundary noise.
Emits extract_plan_39.csv + a validation report. No DB writes (review first).
"""
import re, csv
from collections import Counter

IMA = "D:/workbuddy/2026-08-06-15-31-48/ima_text"
ANSWER = {"upper": f"{IMA}/answer_upper.txt", "lower": f"{IMA}/answer_lower.txt"}

FW_LATIN = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]")
FW_PUNCT = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]")
PUA = re.compile(r"[\uE000-\uF8FF]")
GARBAGE_THRESH = 0.006

def garb(t):
    n = len(t)
    if n == 0: return 0.0
    return (len(FW_LATIN.findall(t)) + len(FW_PUNCT.findall(t)) + len(PUA.findall(t))) / n

def load(vol):
    return open(ANSWER[vol], encoding="utf-8").read()

def volume_of(es):
    return "upper" if int(es.split(".")[0]) <= 4 else "lower"

def section_span(text, es):
    hs = [m.start() for m in re.finditer(r"习题\s*" + re.escape(es) + r"\b", text)]
    if not hs:
        return None, None
    start = hs[-1]
    rest = text[start + 1:]
    end = len(text)
    for m in re.finditer(r"习题\s*\d+\.\d+", rest):     # next exercise header
        end = start + 1 + m.start(); break
    for m in re.finditer(r"\n\s*总习题", rest):          # end-of-chapter
        end = min(end, start + 1 + m.start()); break
    return start, end

def prob_pattern(n):
    return re.compile(r"(?:^|(?<=\n))\s*" + str(n) + r"\.(?=\D)")

def get_problem(text, es, n):
    start, end = section_span(text, es)
    if start is None:
        return None
    seg = text[start:end]
    m = prob_pattern(n).search(seg)
    if not m:
        return None
    ps = m.start()
    nm = prob_pattern(n + 1).search(seg[ps + 1:])
    pe = ps + 1 + nm.start() if nm else len(seg)
    return seg[ps:pe]

def clean_block(b):
    if not b:
        return ""
    out = []
    for ln in b.split("\n"):
        s = ln.strip()
        if s == "" or re.fullmatch(r"\d{1,3}", s) or s.startswith("#") or "$\\blacksquare$" in s:
            continue
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

def split_block(block):
    """restatement = problem text (before solution marker); solution = from marker on."""
    m = re.search(r"\n\s*(?:解|证)\b", block)
    if m:
        return block[:m.start()].strip(), block[m.start():].strip(), block.strip()
    return block.strip(), block.strip(), block.strip()

def over_extracted(block):
    return len(re.findall(r"(?:^|(?<=\n))\s*\d+\.(?=\D)", block)) > 1

def extract(es, n):
    text = load(volume_of(es))
    raw = get_problem(text, es, n)
    if not raw:
        return "", "", ""
    block = clean_block(raw)
    restatement, solution, full = split_block(block)
    return restatement, solution, full

TARGETS = [
    ("1.1",9),("1.2",6),("1.5",8),("2.1",14),("2.2",4),("2.2",7),("2.8",3),
    ("3.5",5),("3.5",7),("3.5",8),("3.6",6),("3.6",7),("3.8",4),
    ("6.2",9),("6.3",1),("6.3",4),("6.5",4),("6.5",5),("6.6",2),("6.8",5),
    ("7.2",2),("7.3",10),("7.3",12),("7.4",1),("7.4",5),
    ("8.2",5),("8.2",9),("8.4",4),("8.6",5),("8.6",6),("8.6",7),("8.6",8),
    ("9.1",1),("9.1",6),("9.2",1),("9.3",2),("9.3",3),("9.5",3),("9.7",2),
]

rows = []
for es, n in TARGETS:
    c, a, full = extract(es, n)
    cg, ag = garb(c), garb(a)
    c_over, a_over = over_extracted(c), over_extracted(a)
    cok = c and cg < GARBAGE_THRESH and "�" not in c and c.startswith(str(n) + ".") and not c_over
    aok = a and ag < GARBAGE_THRESH and "�" not in a and not a_over
    flag = "OK" if (cok and aok) else \
           "CONTENT_OVER" if (c_over and not cok) else \
           "CONTENT_BAD" if not cok else \
           "ANSWER_BAD" if not aok else "BAD"
    rows.append((es, n, c, a, full, cg, ag, flag))
    print(f"{es}#{n}: {flag:12s} clen={len(c)} cg={cg:.4f} alen={len(a)} ag={ag:.4f}")
    if not cok and c:
        print("   C:", repr(c[:130]))
    if not aok and a:
        print("   A:", repr(a[:130]))

with open("D:/workbuddy/2026-08-06-15-31-48/extract_plan_39.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["section","problem","content_text","std_answer","full_solution","content_garb","answer_garb","flag"])
    for es, n, c, a, full, cg, ag, flag in rows:
        w.writerow([es, n, c, a, full, f"{cg:.5f}", f"{ag:.5f}", flag])

print("\nWrote extract_plan_39.csv | counts:", dict(Counter(r[7] for r in rows)))
