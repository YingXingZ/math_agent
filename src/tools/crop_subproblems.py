# -*- coding: utf-8 -*-
"""
双栏小题切分器
教材第6题为双栏排版（(1)(3)(5)... 左栏，(2)(4)(6)... 右栏）
需按行分组后左右切开，才能为每个小题单独分配答题空间
"""
import fitz, sys, os, re, json
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
sys.stdout.reconfigure(encoding='utf-8')

ocr = RapidOCR()
base = r"D:/My File/大四/高数教材答案/"
doc = fitz.open(base + "李继成高数-教材-上册-2版(1).pdf")
DPI, PAGE = 300, 45
OUT = "extract_img/subproblems"
os.makedirs(OUT, exist_ok=True)

pix = doc[PAGE].get_pixmap(dpi=DPI)
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
W, H = img.size
res, _ = ocr(np.array(img))

items = []
for box, text, score in res:
    ys = [p[1] for p in box]; xs = [p[0] for p in box]
    items.append({"text": text, "y0": min(ys), "y1": max(ys),
                  "x0": min(xs), "x1": max(xs)})

# 第6题区间：从 "6. 计算下列极限" 到 "7. 设 fn(x)"
q6_start = q6_end = None
for it in items:
    t = it["text"].replace(" ", "")
    if q6_start is None and re.match(r'^6[\.．]', t):
        q6_start = it["y1"]
    if q6_start is not None and q6_end is None and re.match(r'^7[\.．]', t) and it["y0"] > q6_start:
        q6_end = it["y0"]
print(f"第6题区间 y: {q6_start:.0f} -> {q6_end:.0f}")

# 找所有 (N) 小题标记
marks = []
for it in items:
    t = it["text"].strip()
    if not (q6_start <= it["y0"] <= q6_end):
        continue
    m = re.match(r'^[\(（]\s*(\d{1,2})\s*[\)）]$', t)
    if m:
        marks.append({"no": int(m.group(1)), "y0": it["y0"], "y1": it["y1"],
                      "x0": it["x0"], "x1": it["x1"]})

marks.sort(key=lambda m: (m["y0"], m["x0"]))
print(f"\nOCR 直接识别到 {len(marks)} 个小题标记:")

# --- 栏分界线：右栏标记起始 x 往左留余量（左栏内容一直延伸到右栏之前）---
xs_all = sorted(m["x0"] for m in marks)
gaps = [(xs_all[i+1] - xs_all[i], i) for i in range(len(xs_all)-1)]
max_gap, gi = max(gaps) if gaps else (0, 0)
LEFT_START = xs_all[0]
RIGHT_START = xs_all[gi+1] if max_gap > 300 else None
COL_SPLIT = (RIGHT_START - 25) if RIGHT_START else W
for m in marks:
    col = "左栏" if m["x0"] < COL_SPLIT else "右栏"
    print(f"   ({m['no']:>2})  y={m['y0']:6.0f}  x={m['x0']:6.0f}  {col}")
print(f"\n栏分界 x = {COL_SPLIT:.0f}  (左栏起点 {LEFT_START:.0f}, 右栏起点 {RIGHT_START:.0f})")

# 按 y 聚类成行（容差 45px）
rows = []
for m in marks:
    placed = False
    for r in rows:
        if abs(r["y"] - m["y0"]) < 45:
            r["items"].append(m); placed = True; break
    if not placed:
        rows.append({"y": m["y0"], "items": [m]})
rows.sort(key=lambda r: r["y"])
for r in rows:
    r["items"].sort(key=lambda m: m["x0"])

# --- 缺号补齐：OCR 可能漏识别被公式压住的编号 ---
found = sorted(m["no"] for m in marks)
expected = list(range(1, max(found) + 1))
missing = [n for n in expected if n not in found]
if missing:
    print(f"\n[补齐] 检测到缺失小题编号: {missing}")
    for n in missing:
        # 奇数在左栏、偶数在右栏（该教材双栏顺序），定位到同行对称位置
        target_col_x = LEFT_START if n % 2 == 1 else RIGHT_START
        # 找同伴号（n+1 或 n-1）所在的行
        partner = n + 1 if (n + 1) in found else n - 1
        prow = next((r for r in rows if any(m["no"] == partner for m in r["items"])), None)
        if prow:
            marks.append({"no": n, "y0": prow["y"], "y1": prow["y"] + 40,
                          "x0": target_col_x, "x1": target_col_x + 40})
            prow["items"].append({"no": n, "y0": prow["y"], "x0": target_col_x})
            prow["items"].sort(key=lambda m: m["x0"])
            print(f"   -> ({n}) 补至 行y={prow['y']:.0f} "
                  f"{'左' if n%2==1 else '右'}栏 x={target_col_x:.0f}（依据同行小题 ({partner})）")

print(f"\n聚类为 {len(rows)} 行:")
for i, r in enumerate(rows):
    print(f"   行{i+1} y={r['y']:.0f}: " + " ".join(f"({m['no']})" for m in r["items"]))

# 切分：每行按栏切，行的 y 范围到下一行起点
CONTENT_X0, CONTENT_X1 = int(LEFT_START) - 20, W - 120
meta = []
for i, r in enumerate(rows):
    y0 = r["y"] - 15
    y1 = (rows[i + 1]["y"] - 15) if i + 1 < len(rows) else q6_end - 10
    for m in r["items"]:
        if m["x0"] < COL_SPLIT:
            x0, x1 = CONTENT_X0, int(COL_SPLIT)
        else:
            x0, x1 = int(RIGHT_START) - 20, CONTENT_X1
        crop = img.crop((x0, int(y0), x1, int(y1)))
        # 去除周边纯白边，让裁切更紧凑
        arr = np.array(crop.convert("L"))
        mask = arr < 220
        if mask.any():
            rs = np.where(mask.any(axis=1))[0]
            cs = np.where(mask.any(axis=0))[0]
            crop = crop.crop((max(0, cs[0] - 8), max(0, rs[0] - 8),
                              min(crop.width, cs[-1] + 8), min(crop.height, rs[-1] + 8)))
        fn = f"{OUT}/q6_sub{m['no']:02d}.png"
        crop.save(fn)
        meta.append({"parent": "6", "no": f"({m['no']})", "img": fn,
                     "w": crop.width, "h": crop.height})
        print(f"   -> ({m['no']:>2}) {fn}  {crop.width}x{crop.height}")

meta.sort(key=lambda x: int(x["no"].strip("()")))
json.dump(meta, open(f"{OUT}/meta.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print(f"\n共切出 {len(meta)} 个小题")
doc.close()
