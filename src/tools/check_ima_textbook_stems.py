# -*- coding: utf-8 -*-
"""Test whether IMA textbook OCR text (李继成 教材) has CLEAN stems for the 27 blocked
problems. If stems are clean (low garbage_score), we can recover them from text without
rendering PDFs / calling VLM for the stem. Answers still need VLM or IMA answer text.
"""
import re, os

FW = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]"); FP = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]"); PUA = re.compile(r"[\uE000-\uF8FF]")
def gs(t):
    n=len(t); return 0.0 if n==0 else (len(FW.findall(t))+len(FP.findall(t))+len(PUA.findall(t)))/n

UPPER = r"D:\workbuddy\2026-08-06-15-31-48\ima_text\textbook_upper.txt"
LOWER = r"D:\workbuddy\2026-08-06-15-31-48\ima_text\textbook_lower.txt"

def vol_for(sec):
    n = int(sec.split(".")[0])
    return UPPER if n <= 4 else LOWER

def section_span(text, es):
    hs = [m.start() for m in re.finditer(r"习题\s*" + re.escape(es) + r"\b", text)]
    if not hs: return None, None
    start = hs[-1]
    rest = text[start+1:]; end = len(text)
    for m in re.finditer(r"习题\s*\d+\.\d+", rest):
        end = start+1+m.start(); break
    for m in re.finditer(r"\n\s*总习题", rest):
        end = min(end, start+1+m.start()); break
    return start, end

def prob_pattern(n):
    return re.compile(r"(?:^|(?<=\n))\s*" + str(n) + r"\.(?=\D)")

# 27 targets
targets = [("2.8","3"),("3.5","5"),("3.5","7"),("3.5","8"),("3.6","6"),("3.6","7"),("3.8","4"),
           ("6.8","5"),("7.2","2"),("7.3","10"),("7.3","12"),("7.4","1"),("7.4","5"),
           ("8.2","5"),("8.2","9"),("8.4","4"),("8.6","5"),("8.6","6"),("8.6","7"),("8.6","8"),
           ("9.1","1"),("9.1","6"),("9.2","1"),("9.3","2"),("9.3","3"),("9.5","3"),("9.7","2")]

for sec, no in targets:
    text = open(vol_for(sec), encoding="utf-8").read()
    s, e = section_span(text, sec)
    if s is None:
        print(f"S{sec}#{no}: SECTION NOT FOUND in text"); continue
    block = text[s:e]
    m = prob_pattern(no).search(block)
    if not m:
        print(f"S{sec}#{no}: problem marker not found"); continue
    bstart = m.start()
    # find next problem marker
    nxt = prob_pattern(r"\d+").search(block, bstart+1)
    bend = nxt.start() if nxt else len(block)
    raw = block[bstart:bend].strip()
    # stem = before 解/证
    sm = re.search(r"\n\s*(?:解|证)\b", raw)
    stem = raw[:sm.start()].strip() if sm else raw
    g = gs(stem)
    print(f"S{sec}#{no}: stem_len={len(stem)} gs={g:.4f} repl={'Y' if '\ufffd' in stem else 'N'}  head={stem[:50]!r}")
