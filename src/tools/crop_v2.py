# -*- coding: utf-8 -*-
"""按题号切分习题区，输出每题裁切图 + 结构化元数据"""
import fitz, sys, os, re, json
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
sys.stdout.reconfigure(encoding='utf-8')

ocr = RapidOCR()
base = r"D:/My File/大四/高数教材答案/"
doc = fitz.open(base + "李继成高数-教材-上册-2版(1).pdf")
DPI, PAGE = 300, 45
os.makedirs("extract_img/problems", exist_ok=True)

pix = doc[PAGE].get_pixmap(dpi=DPI)
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
W, H = img.size
res, _ = ocr(np.array(img))
items = []
for box, text, score in res:
    ys=[p[1] for p in box]; xs=[p[0] for p in box]
    items.append({"text":text,"y0":min(ys),"y1":max(ys),"x0":min(xs),"x1":max(xs)})
items.sort(key=lambda it: it["y0"])

# 习题区边界
start_y, end_y = None, None
for it in items:
    t = it["text"].replace(" ","")
    if start_y is None and re.search(r'习题1\.3', t): start_y = it["y1"] + 10
    if start_y is not None and end_y is None and re.match(r'^1\.4', t) and it["y0"] > start_y: end_y = it["y0"] - 15

# 找题号锚点：行首形如 "N." 且位于左栏起始 x
LEFT_X_MAX = 400
anchors = []
for it in items:
    if not (start_y <= it["y0"] <= end_y): continue
    if it["x0"] > LEFT_X_MAX: continue
    m = re.match(r'^\s*(\d{1,2})\s*[\.．、]', it["text"])
    if m:
        n = int(m.group(1))
        if 1 <= n <= 20:
            anchors.append({"no": n, "y": it["y0"], "text": it["text"][:60]})
# 去重：同题号保留最靠上
seen, uniq = set(), []
for a in sorted(anchors, key=lambda x:x["y"]):
    if a["no"] in seen: continue
    seen.add(a["no"]); uniq.append(a)
uniq.sort(key=lambda x:x["no"])

print(f"习题区 y: {start_y} - {end_y}")
print(f"识别到 {len(uniq)} 道大题:")
for a in uniq: print(f"   第{a['no']}题 y={a['y']:.0f}  {a['text']}")

# 裁切
PAD_X = 30
meta = []
for i, a in enumerate(uniq):
    y0 = max(0, a["y"] - 12)
    y1 = uniq[i+1]["y"] - 12 if i+1 < len(uniq) else end_y
    crop = img.crop((int(200-PAD_X), int(y0), int(W-100), int(y1)))
    fn = f"extract_img/problems/hw1_3_p{a['no']}.png"
    crop.save(fn)
    meta.append({"no": str(a["no"]), "img": fn, "y0": y0, "y1": y1, "height_px": int(y1-y0)})
    print(f"   -> saved {fn} ({crop.width}x{crop.height})")

# 整区裁切
whole = img.crop((170, int(start_y), W-100, int(end_y)))
whole.save("extract_img/problems/hw1_3_whole.png")
print(f"\n整区图: {whole.width}x{whole.height}")
json.dump(meta, open("extract_img/problems/meta.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
doc.close()
