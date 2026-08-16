# -*- coding: utf-8 -*-
"""定位习题区域并按题号切分 —— 题目提取核心算法验证"""
import fitz, sys, os, re, json
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
sys.stdout.reconfigure(encoding='utf-8')

ocr = RapidOCR()
base = r"D:/My File/大四/高数教材答案/"
doc = fitz.open(base + "李继成高数-教材-上册-2版(1).pdf")
DPI = 300
PAGE = 45  # 教材页24，习题1.3

pix = doc[PAGE].get_pixmap(dpi=DPI)
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
img.save("extract_img/hw13_full.png")
W, H = img.size
print(f"page size: {W} x {H} @ {DPI}dpi")

res, _ = ocr(np.array(img))
# res: [[box, text, score], ...]  box=[[x0,y0],[x1,y1],[x2,y2],[x3,y3]]
items = []
for box, text, score in res:
    ys = [p[1] for p in box]; xs = [p[0] for p in box]
    items.append({"text": text, "y0": min(ys), "y1": max(ys), "x0": min(xs), "x1": max(xs), "score": score})
items.sort(key=lambda it: it["y0"])

print("\n--- 前30个文本块及坐标 ---")
for it in items[:30]:
    print(f"y={it['y0']:6.0f}-{it['y1']:6.0f} x={it['x0']:6.0f} | {it['text'][:50]}")

# 定位习题区起止
start_y = end_y = None
for it in items:
    t = it["text"].replace(" ", "")
    if start_y is None and re.search(r'习题1\.3', t):
        start_y = it["y0"]
    if start_y is not None and end_y is None and re.match(r'^1\.4', t):
        end_y = it["y0"]
print(f"\n习题区 y范围: {start_y} -> {end_y}")
json.dump(items, open("ocr_items_p45.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
doc.close()
