# -*- coding: utf-8 -*-
import fitz, sys
sys.stdout.reconfigure(encoding='utf-8')
base = r"D:/My File/大四/高数教材答案/"
d = fitz.open(base+"李继成高数-教材-上册-2版(1).pdf")
print("page size:", d[10].rect)
for p in [8, 9, 45, 46]:
    pix = d[p].get_pixmap(dpi=150)
    pix.save(f"probe_img/textbook_p{p}.png")
    print("saved", p, pix.width, "x", pix.height)
d.close()
d2 = fitz.open(base+"李继成高数-答案-上册(1).pdf")
pix = d2[20].get_pixmap(dpi=150); pix.save("probe_img/answer_p20.png")
print("answer page saved", d2.page_count)
d2.close()
