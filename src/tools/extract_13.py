# -*- coding: utf-8 -*-
import fitz, sys, os
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
sys.stdout.reconfigure(encoding='utf-8')
ocr = RapidOCR()
base = r"D:/My File/大四/高数教材答案/"
doc = fitz.open(base + "李继成高数-教材-上册-2版(1).pdf")
os.makedirs("extract_img", exist_ok=True)
for p in range(43, 48):
    pix = doc[p].get_pixmap(dpi=200)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    img.save(f"extract_img/p{p}.png")
    res, _ = ocr(np.array(img))
    txt = "\n".join([r[1] for r in res]) if res else ""
    print(f"########## PDF idx {p} (教材页约 {p-21}) ##########")
    print(txt)
    print()
doc.close()
