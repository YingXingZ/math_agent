# -*- coding: utf-8 -*-
import fitz, sys, io, re, json
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
sys.stdout.reconfigure(encoding='utf-8')

ocr = RapidOCR()
base = r"D:/My File/大四/高数教材答案/"
doc = fitz.open(base + "李继成高数-教材-上册-2版(1).pdf")

def page_text(pno, dpi=150):
    pix = doc[pno].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    res, _ = ocr(np.array(img))
    if not res: return ""
    return "\n".join([r[1] for r in res])

# 扫描前 20 页找目录
for p in range(0, 20):
    t = page_text(p)
    has_toc = ('目' in t and '录' in t) or t.count('...') > 3 or re.search(r'第.{1,3}章', t)
    print(f"--- page {p} (len={len(t)}) toc_like={has_toc} ---")
    print(t[:400])
    print()
doc.close()
