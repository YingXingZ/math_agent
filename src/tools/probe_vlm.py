# -*- coding: utf-8 -*-
"""Probe for VLM re-OCR: list corrupted (content-FW) problems minus the 2 released,
show their crop_image_path, check image existence locally, and ping 18080/health.
"""
import re, sqlite3, os, json, urllib.request

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
IMAGE_ROOT = r"D:\workbuddy\2026-08-06-15-31-48\extract_img"
VLM = "http://127.0.0.1:18080"
WHITELIST = {("6.2", "4", ""), ("6.7", "8", "")}

FW = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]"); FP = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]"); PUA = re.compile(r"[\uE000-\uF8FF]")
def gs(t):
    n = len(t); return 0.0 if n == 0 else (len(FW.findall(t)) + len(FP.findall(t)) + len(PUA.findall(t))) / n

s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
rows = s.execute("""SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.crop_image_path, s.section_no
                    FROM problems p JOIN sections s ON s.id=p.section_id""").fetchall()

corrupt = []
for r in rows:
    if gs(r["content_text"] or "") > 0.006:
        key = (str(r["section_no"]), str(r["problem_no"]), r["sub_no"] or "")
        if key in WHITELIST:
            continue
        corrupt.append(r)

print(f"corrupted (content-FW, excl. 2 released): {len(corrupt)}")
have_img = 0
for r in corrupt:
    cp = r["crop_image_path"] or ""
    # resolve path
    if cp and not os.path.isabs(cp):
        cand = os.path.join(IMAGE_ROOT, cp)
    else:
        cand = cp
    exists = os.path.exists(cand)
    if exists:
        have_img += 1
    if len(corrupt) <= 6 or not exists:
        print(f"  S{r['section_no']}#{r['problem_no']}{r['sub_no'] or ''} img_exists={exists} path={cp!r}")

print(f"with existing local image: {have_img}/{len(corrupt)}")

# ping VLM
try:
    with urllib.request.urlopen(VLM + "/health", timeout=8) as resp:
        print("VLM /health:", resp.status, resp.read().decode()[:200])
except Exception as e:
    print("VLM /health ERROR:", repr(e))
