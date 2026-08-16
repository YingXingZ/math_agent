# -*- coding: utf-8 -*-
import fitz, sys, re
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
    return "\n".join([r[1] for r in res]) if res else ""
out=[]
for p in range(12, 26):
    t = page_text(p)
    out.append(f"===== PDF page index {p} (len={len(t)}) =====\n{t[:700]}\n")
open("toc_dump.txt","w",encoding="utf-8").write("\n".join(out))
print("\n".join(out))
doc.close()
