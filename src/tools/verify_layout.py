# -*- coding: utf-8 -*-
"""排版量化校验：越界检测 + 留白分析 + 墨迹密度"""
import fitz, sys
import numpy as np
sys.stdout.reconfigure(encoding='utf-8')

A4_W, A4_H = 595.28, 841.89
ML, MR, MT, MB = 56.7, 45.4, 56.7, 51.0

doc = fitz.open("outputs/习题1.3_作业卷_A4.pdf")
print(f"总页数: {doc.page_count}")
ok = True
for i, page in enumerate(doc):
    print(f"\n===== 第 {i+1} 页 =====")
    r = page.rect
    print(f"  尺寸: {r.width:.1f} x {r.height:.1f} pt  (A4标准 595.3 x 841.9)")
    # 图片元素
    imgs = page.get_image_info()
    for j, im in enumerate(imgs):
        b = fitz.Rect(im["bbox"])
        over = []
        if b.x0 < ML - 1: over.append(f"左越界{ML-b.x0:.1f}pt")
        if b.x1 > A4_W - MR + 1: over.append(f"右越界{b.x1-(A4_W-MR):.1f}pt")
        if b.y0 < MT - 20: over.append(f"上越界{MT-b.y0:.1f}pt")
        if b.y1 > A4_H - MB + 1: over.append(f"下越界{b.y1-(A4_H-MB):.1f}pt")
        flag = "  ".join(over) if over else "OK"
        if over: ok = False
        print(f"  图{j+1}: x[{b.x0:.0f}-{b.x1:.0f}] y[{b.y0:.0f}-{b.y1:.0f}]  w={b.width:.0f} h={b.height:.0f}  {flag}")
    # 文本块
    txts = page.get_text("blocks")
    for t in txts:
        b = fitz.Rect(t[:4])
        if b.x1 > A4_W - MR + 2 or b.x0 < ML - 2:
            print(f"  [警告] 文本越界: x[{b.x0:.0f}-{b.x1:.0f}] '{t[4][:30].strip()}'")
            ok = False
    # 墨迹密度（渲染后统计非白像素比例，判断留白是否充足）
    pix = page.get_pixmap(dpi=100)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    gray = arr[:, :, :3].mean(axis=2)
    ink = (gray < 200).mean()
    # 按行统计空白带
    row_ink = (gray < 200).mean(axis=1)
    blank_rows = (row_ink < 0.005).sum()
    print(f"  墨迹密度: {ink*100:.1f}%   空白行占比: {blank_rows/pix.height*100:.1f}%  (答题空间指标)")
print("\n" + ("排版校验通过：无越界" if ok else "存在越界问题"))
doc.close()
