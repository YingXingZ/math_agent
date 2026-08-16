# -*- coding: utf-8 -*-
"""
A4 作业卷生成引擎 v2 —— 原图保真版
- 题目图片来自教材原版裁切，公式 100% 保真
- 支持单列/双列布局，按题型分配手写空间
- 自动分页，不切断题目
"""
import fitz, os, sys, json
sys.stdout.reconfigure(encoding='utf-8')

# ---------- A4 版面参数（pt，1pt = 1/72 inch）----------
A4_W, A4_H = 595.28, 841.89
MARGIN_TOP, MARGIN_BOTTOM = 56.7, 51.0     # 2.0cm / 1.8cm
MARGIN_LEFT, MARGIN_RIGHT = 56.7, 45.4     # 2.0cm / 1.6cm
CONTENT_W = A4_W - MARGIN_LEFT - MARGIN_RIGHT
HEADER_H = 80
GUTTER = 18                                 # 双列列间距
COL_W = (CONTENT_W - GUTTER) / 2
DPI = 300

FONT_SONG = "C:/Windows/Fonts/simsun.ttc"
FONT_HEI = "C:/Windows/Fonts/simhei.ttf"


class WorksheetBuilder:
    def __init__(self, title, subtitle, meta_line):
        self.doc = fitz.open()
        self.title, self.subtitle, self.meta_line = title, subtitle, meta_line
        self.page, self.y, self.page_no = None, 0, 0
        self._new_page(first=True)

    # ---------- 页面骨架 ----------
    def _new_page(self, first=False):
        self.page = self.doc.new_page(width=A4_W, height=A4_H)
        self.page_no += 1
        self.page.insert_font(fontname="song", fontfile=FONT_SONG)
        self.page.insert_font(fontname="hei", fontfile=FONT_HEI)
        self.y = MARGIN_TOP
        self._draw_header() if first else self._draw_running_head()
        self._draw_footer()

    def _draw_header(self):
        p, x = self.page, MARGIN_LEFT
        p.insert_text((x, self.y + 16), self.title, fontname="hei", fontsize=16)
        p.insert_text((x, self.y + 36), self.subtitle, fontname="song",
                      fontsize=10, color=(0.35, 0.35, 0.35))
        iy = self.y + 60
        for lbl, off, ln in [("班级：", 0, 96), ("学号：", 148, 122), ("姓名：", 318, 106)]:
            p.insert_text((x + off, iy), lbl, fontname="song", fontsize=10.5)
            p.draw_line(fitz.Point(x + off + 37, iy + 2),
                        fitz.Point(x + off + 37 + ln, iy + 2), width=0.6)
        p.insert_text((A4_W - MARGIN_RIGHT - 152, self.y + 16), self.meta_line,
                      fontname="song", fontsize=8.5, color=(0.45, 0.45, 0.45))
        p.insert_text((A4_W - MARGIN_RIGHT - 152, self.y + 32), "成绩：__________",
                      fontname="song", fontsize=9, color=(0.3, 0.3, 0.3))
        ly = self.y + HEADER_H - 6
        p.draw_line(fitz.Point(MARGIN_LEFT, ly), fitz.Point(A4_W - MARGIN_RIGHT, ly),
                    color=(0.15, 0.15, 0.15), width=1.1)
        self.y += HEADER_H + 6

    def _draw_running_head(self):
        p = self.page
        p.insert_text((MARGIN_LEFT, self.y + 9), self.title,
                      fontname="song", fontsize=8.5, color=(0.5, 0.5, 0.5))
        p.insert_text((A4_W - MARGIN_RIGHT - 96, self.y + 9), "姓名：___________",
                      fontname="song", fontsize=8.5, color=(0.5, 0.5, 0.5))
        p.draw_line(fitz.Point(MARGIN_LEFT, self.y + 16),
                    fitz.Point(A4_W - MARGIN_RIGHT, self.y + 16),
                    color=(0.78, 0.78, 0.78), width=0.5)
        self.y += 28

    def _draw_footer(self):
        txt = f"— {self.page_no} —"
        self.page.insert_text(((A4_W - len(txt) * 6.2) / 2, A4_H - MARGIN_BOTTOM + 22),
                              txt, fontname="song", fontsize=9, color=(0.5, 0.5, 0.5))

    def _remaining(self):
        return A4_H - MARGIN_BOTTOM - self.y

    # ---------- 内容元素 ----------
    def add_section_title(self, text, hint=None):
        need = 26 + (14 if hint else 0)
        if self._remaining() < need + 90:
            self._new_page()
        self.page.insert_text((MARGIN_LEFT, self.y + 12), text, fontname="hei", fontsize=11.5)
        self.y += 20
        if hint:
            self.page.insert_text((MARGIN_LEFT, self.y + 9), hint, fontname="song",
                                  fontsize=8.5, color=(0.5, 0.5, 0.5))
            self.y += 15
        self.y += 4

    def _img_size(self, path, max_w):
        pm = fitz.Pixmap(path)
        w_pt, h_pt = pm.width * 72.0 / DPI, pm.height * 72.0 / DPI
        s = min(1.0, max_w / w_pt)
        return w_pt * s, h_pt * s

    def add_problem(self, img_path, answer_space):
        """单列题目：整宽题干 + 手写区"""
        dw, dh = self._img_size(img_path, CONTENT_W)
        block = dh + answer_space + 12
        if self._remaining() < block:
            if self._remaining() < dh + 55:
                self._new_page()
            else:
                answer_space = max(48, self._remaining() - dh - 14)
        self.page.insert_image(
            fitz.Rect(MARGIN_LEFT, self.y, MARGIN_LEFT + dw, self.y + dh),
            filename=img_path)
        self.y += dh + 5
        bot = min(self.y + answer_space, A4_H - MARGIN_BOTTOM)
        self.page.draw_rect(fitz.Rect(MARGIN_LEFT, self.y, A4_W - MARGIN_RIGHT, bot),
                            color=(0.87, 0.87, 0.87), width=0.5, dashes="[2 3] 0")
        self.y = bot + 11

    def add_problem_pair(self, left_img, right_img, answer_space):
        """双列题目：一行放两个小题，各自独立手写区"""
        lw, lh = self._img_size(left_img, COL_W - 6)
        rw, rh = (self._img_size(right_img, COL_W - 6) if right_img else (0, 0))
        head_h = max(lh, rh)
        block = head_h + answer_space + 12
        if self._remaining() < block:
            self._new_page()
        y_top = self.y
        self.page.insert_image(
            fitz.Rect(MARGIN_LEFT, y_top, MARGIN_LEFT + lw, y_top + lh),
            filename=left_img)
        if right_img:
            rx = MARGIN_LEFT + COL_W + GUTTER
            self.page.insert_image(fitz.Rect(rx, y_top, rx + rw, y_top + rh),
                                   filename=right_img)
        ay = y_top + head_h + 4
        bot = min(ay + answer_space, A4_H - MARGIN_BOTTOM)
        self.page.draw_rect(fitz.Rect(MARGIN_LEFT, ay, MARGIN_LEFT + COL_W, bot),
                            color=(0.87, 0.87, 0.87), width=0.5, dashes="[2 3] 0")
        if right_img:
            rx = MARGIN_LEFT + COL_W + GUTTER
            self.page.draw_rect(fitz.Rect(rx, ay, rx + COL_W, bot),
                                color=(0.87, 0.87, 0.87), width=0.5, dashes="[2 3] 0")
        self.y = bot + 11

    def save(self, path):
        self.doc.save(path, garbage=4, deflate=True)
        n = self.doc.page_count
        self.doc.close()
        return path, n


def main():
    main_meta = json.load(open("extract_img/problems/meta.json", encoding="utf-8"))
    sub_meta = json.load(open("extract_img/subproblems/meta.json", encoding="utf-8"))
    by_no = {m["no"]: m for m in main_meta}
    subs = sorted(sub_meta, key=lambda x: int(x["no"].strip("()")))

    wb = WorksheetBuilder(
        title="高等数学（上册）课后作业",
        subtitle="第一章 极限与连续 · 1.3 极限的性质以及运算法则 · 习题 1.3",
        meta_line="教材：李继成《高等数学》第二版"
    )

    # 一、证明题（1-5），每题给足推导空间
    wb.add_section_title("一、证明题（第 1 ~ 5 题，每题 10 分）",
                         "要求写出完整推导过程，关键步骤需说明依据。")
    for no, space in [("1", 132), ("2", 145), ("3", 150), ("4", 130), ("5", 135)]:
        wb.add_problem(by_no[no]["img"], space)

    # 二、计算题（第6题12小题），双列排布
    wb.add_section_title("二、计算题（第 6 题，共 12 小题，每小题 3 分）",
                         "请在每小题下方虚线区域内作答，写出主要计算步骤。")
    for i in range(0, len(subs), 2):
        left = subs[i]["img"]
        right = subs[i + 1]["img"] if i + 1 < len(subs) else None
        wb.add_problem_pair(left, right, 104)

    # 三、综合题（7-8）
    wb.add_section_title("三、综合题（第 7 ~ 8 题，每题 10 分）")
    for no, space in [("7", 150), ("8", 150)]:
        wb.add_problem(by_no[no]["img"], space)

    os.makedirs("outputs", exist_ok=True)
    out, n = wb.save("outputs/习题1.3_作业卷_A4.pdf")
    print(f"生成完成: {out}   共 {n} 页")

    d = fitz.open(out)
    for i in range(d.page_count):
        d[i].get_pixmap(dpi=100).save(f"outputs/preview_p{i+1}.png")
    d.close()
    print(f"预览图已输出 {n} 张")


if __name__ == "__main__":
    main()
