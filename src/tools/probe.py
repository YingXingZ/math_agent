# -*- coding: utf-8 -*-
import fitz, sys, io
sys.stdout.reconfigure(encoding='utf-8')
base = r"D:/My File/大四/高数教材答案/"
files = ["李继成高数-教材-上册-2版(1).pdf", "李继成高数-答案-上册(1).pdf"]
for f in files:
    d = fitz.open(base+f)
    print("="*60)
    print(f, "| pages:", d.page_count)
    toc = d.get_toc()
    print("TOC entries:", len(toc))
    for t in toc[:25]:
        print("   ", t)
    # 文本层检测
    for p in [20, 60, 100]:
        if p < d.page_count:
            txt = d[p].get_text()
            print(f"--- page {p} textlen={len(txt)} ---")
            print(txt[:300].replace("\n"," | "))
    d.close()
