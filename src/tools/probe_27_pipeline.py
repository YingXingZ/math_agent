# -*- coding: utf-8 -*-
"""Probe the 27 still-blocked (content-FW) problems:
 - current content_text garbage, answer_status, std_answer presence
 - crop_image_path from DB + whether it exists
 - whether extract_img/book/<sec>/ has a crop (vlm_reoc_8014 path)
 - whether a local PDF exists to render from (fallback)
"""
import re, sqlite3, os, json

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
IMAGE_ROOT = r"D:\workbuddy\2026-08-06-15-31-48\extract_img"
PDF_DIR = r"D:\My File\大四\高数教材答案"

FW = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]"); FP = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]"); PUA = re.compile(r"[\uE000-\uF8FF]")
def gs(t):
    n = len(t); return 0.0 if n == 0 else (len(FW.findall(t)) + len(FP.findall(t)) + len(PUA.findall(t))) / n

# local PDF sources (李继成/同济 上/下册 教材 + 答案)
pdfs = []
for root, _, files in os.walk(PDF_DIR):
    for f in files:
        if f.lower().endswith(".pdf"):
            pdfs.append(os.path.join(root, f))
print(f"=== local PDFs ({len(pdfs)}) ===")
for p in pdfs:
    print("  ", p)

s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
rows = s.execute("""SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.std_answer, p.answer_status, p.crop_image_path, s.section_no
                    FROM problems p JOIN sections s ON s.id=p.section_id""").fetchall()

print(f"\n=== problems with content garbage (gs>0.006): N={sum(1 for r in rows if gs(r['content_text'] or '')>0.006)} ===")
blocked = []
for r in rows:
    g = gs(r["content_text"] or "")
    if g <= 0.006:
        continue
    sec = str(r["section_no"]); no = str(r["problem_no"]); sub = r["sub_no"] or ""
    cp = r["crop_image_path"] or ""
    # resolve crop path
    cand = cp if os.path.isabs(cp) else (os.path.join(IMAGE_ROOT, cp) if cp else "")
    cp_exists = bool(cand) and os.path.exists(cand)
    # extract_img/book/<sec>/ pattern
    n, m = sec.split(".")
    book_cand = os.path.join(IMAGE_ROOT, "book", sec, f"p{n}_{m}_p{no}")
    book_exists = any(os.path.exists(book_cand + ext) for ext in (".png",".jpg",".jpeg"))
    has_ans = bool((r["std_answer"] or "").strip())
    blocked.append((sec, no, sub, g, r["answer_status"], has_ans, cp_exists, book_exists, cp))
    print(f"  S{sec}#{no}{sub} gs={g:.4f} status={r['answer_status']} ans={'Y' if has_ans else 'N'} "
          f"cp_exists={cp_exists} book_exists={book_exists} cp={cp!r}")

print(f"\nTOTAL blocked(gs>0.006) = {len(blocked)}")
with_img = sum(1 for b in blocked if b[6] or b[7])
print(f"  with any local image (crop or book) = {with_img}")
print(f"  no local image at all = {len(blocked)-with_img}")
