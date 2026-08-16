# -*- coding: utf-8 -*-
import fitz, sys
sys.stdout.reconfigure(encoding='utf-8')
base = r"D:/My File/大四/高数教材答案/"
d = fitz.open(base+"李继成高数-教材-上册-2版(1).pdf")
for p in range(2,7):
    pix = d[p].get_pixmap(dpi=120)
    pix.save(f"probe_img/textbook_p{p}.png")
    print("saved", p)
d.close()
