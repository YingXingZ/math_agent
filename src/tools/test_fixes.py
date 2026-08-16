# -*- coding: utf-8 -*-
"""针对 PDF page 53（习题1.4）的修复验证脚本"""
import sys, re, json, os
import fitz
import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

import extract_prototype as ep

PDF = r"D:/My File/大四/高数教材答案/李继成高数-教材-上册-2版(1).pdf"
PAGE = 53
DPI = 300
OUT = "extract_img_test"

os.makedirs(OUT, exist_ok=True)
detect = ep.build_ocr("rapidocr")

print(f"=== 测试: PDF page {PAGE} @ {DPI}dpi ===", flush=True)

doc = fitz.open(PDF)
img = ep.render_page(doc, PAGE, DPI)
W, H = img.size
print(f"页面尺寸: {W}x{H}", flush=True)

items = detect(np.array(img))
items.sort(key=lambda it: it["y0"])
print(f"OCR 识别: {len(items)} 个文本块", flush=True)

# ---------- 测试 _is_body_heading（新版带 x0 参数）----------
print("\n--- 测试 _is_body_heading (带 x0 过滤) ---", flush=True)

def _is_body_heading_v2(text, y, H, x0=0):
    """同 extract_book.py 中的修复版"""
    t = text.strip()
    if re.search(r"习题\s*\d+\.\d+", t):
        return False
    if re.match(r"^[第\s]*[\d一二三四五六七八九十]+[章\s]", t):
        return True
    if y < 120:
        return False
    if x0 > 350:
        return False
    if re.match(r"^\s*\d+\.\d+(\.\d+)?\s*\D", t):
        return True
    if y >= 120 and re.match(r"^\s*\d+\.\d+(\.\d+)?\s*$", t):
        return True
    return False

false_positives = []
for it in items:
    y, x0, t = it["y0"], it["x0"], it["text"].strip()
    if re.match(r"^\s*\d+\.\d+", t):
        old_result = bool(re.match(r"^\s*\d+\.\d+(\.\d+)?(\s*\D|\s*$)", t) and y >= 120)
        new_result = _is_body_heading_v2(it["text"], y, H, x0)
        if old_result or new_result:
            false_positives.append((y, x0, t[:60], old_result, new_result))

if false_positives:
    print("  匹配 'X.Y' 模式的文本块（旧/新判定）:")
    for y, x0, t, old, new in false_positives:
        tag = "✓固定" if old != new else ("仍误判" if new else "正确排除")
        print(f"    y={y:.0f} x0={x0:.0f} [{tag}] 旧={old} 新={new}  '{t}'")
else:
    print("  无匹配 'X.Y' 模式的文本块")

# ---------- 测试 find_problem_anchors（新版正则含：）----------
print("\n--- 测试 find_problem_anchors (新版正则) ---", flush=True)

# 先找到 "习题1.4" 位置
exercise_y = None
for it in items:
    if re.search(r"习题\s*1\.4", it["text"].replace(" ", "")):
        exercise_y = it["y1"] + 10
        print(f"找到 习题1.4 at y={it['y0']:.0f}", flush=True)
        break

if exercise_y is None:
    print("ERROR: 未找到 习题1.4！")
    for it in items:
        t = it["text"].strip()
        if re.search(r"习题", t):
            print(f"  y={it['y0']:.0f} x0={it['x0']:.0f} '{t[:60]}'")
else:
    anchors = ep.find_problem_anchors(items, exercise_y, H)
    print(f"习题区 [{exercise_y:.0f}..{H}] 找到 {len(anchors)} 个大题锚点:")
    for a in anchors:
        print(f"  #{a['no']} y={a['y']:.0f} '{a['text']}'")

# ---------- 列出范围内所有的疑似题号 ----------
print("\n--- 范围内所有疑似题号文本 ---")
for it in items:
    if it["y0"] >= exercise_y:
        t = it["text"].strip()
        m_new = re.match(r"^\s*(\d{1,2})\s*[\.．、：]", t)
        m_old = re.match(r"^\s*(\d{1,2})\s*[\.．、]", t)
        if m_new:
            flag = " ★新匹配（旧正则漏掉）" if not m_old else ""
            print(f"  #{m_new.group(1)} y={it['y0']:.0f} x0={it['x0']:.0f} '{t[:50]}'{flag}")

# ---------- 测试 _extract_band ----------
print("\n--- 测试 _extract_band ---", flush=True)
from extract_book import _extract_band, section_kp

asec = {"dir": OUT, "problems": []}
_extract_band(img, items, exercise_y, H, "1.4", asec, OUT,
              "p1_4", detect, dual_threshold=2.5)
print(f"提取结果: {len(asec['problems'])} 道大题")
for p in asec["problems"]:
    subs = p.get("subproblems", [])
    print(f"  第{p['no']}题 ({p['height_px']}px) 小题: {[s['no'] for s in subs]}")

doc.close()
print("\n=== 测试完成 ===", flush=True)
