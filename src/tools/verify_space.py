# -*- coding: utf-8 -*-
"""答题区尺寸校验：换算成实际可书写行数"""
import fitz, sys
sys.stdout.reconfigure(encoding='utf-8')
doc = fitz.open("outputs/习题1.3_作业卷_A4.pdf")
PT2MM = 25.4/72
LINE_H_PT = 22   # 手写一行约 7.8mm
total_area = 0
for i, page in enumerate(doc):
    rects=[]
    for d in page.get_drawings():
        if d.get("dashes") and d["dashes"] != "[] 0":
            r = d["rect"]
            if r.width > 60 and r.height > 25:
                rects.append(r)
    print(f"\n第{i+1}页 答题区 {len(rects)} 个:")
    for r in rects:
        lines = r.height / LINE_H_PT
        total_area += r.width*r.height
        w_mm, h_mm = r.width*PT2MM, r.height*PT2MM
        verdict = "充足" if lines>=4 else ("偏紧" if lines>=2.5 else "不足")
        print(f"   {w_mm:5.0f}mm x {h_mm:5.0f}mm  ≈ {lines:.1f} 行手写  [{verdict}]")
page_area = 595.28*841.89*doc.page_count
print(f"\n答题区总面积占比: {total_area/page_area*100:.1f}%")
doc.close()
