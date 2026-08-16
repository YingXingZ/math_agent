# -*- coding: utf-8 -*-
"""生成一份模拟学生作答 PDF（§1.1 六题），用于端到端闭环验证。"""
import os
from fpdf import FPDF

FONT = r"C:/Windows/Fonts/simhei.ttf"
OUT = r"D:/My File/大四/高数教材答案/高数作业助手/data/uploads/student_demo_20231101_王小明.pdf"

PAGES = [
    # (题号标题, 学生作答文本)
    ("1. 求下列函数的定义域",
     "解：\n"
     "(1) x^2 + 3x - 4 >= 0，解得 x <= -4 或 x >= 1，定义域 (-∞,-4] ∪ [1,+∞)。\n"
     "(2) 5 - 2x > 0，解得 x < 5/2，定义域 (-∞, 5/2]。\n"
     "(3) -1 <= x-2 <= 1 且 9 - x^2 > 0，解得 x ∈ [-1,3) 且 x ∈ (-3,3)，交集为 [-1,3)。\n"
     "(4) 3x - 7 ≠ 0，即 x ≠ 7/3，定义域 (-∞, 7/3) ∪ (7/3, +∞)。"),

    ("2. 下列函数是否相同？说明理由",
     "(1) 不同，因为对应法则不同。\n"
     "(2) 不同，因为定义域不同。\n"
     "(3) 相同，因为 g(x) = 1 - cos^2 x = sin^2 x = f(x)，定义域与对应法则均相同。"),

    ("3. 讨论下列函数的奇偶性",
     "(1) f(-x) = 2 - (-x)^2 + 3(-x)^4 = 2 - x^2 + 3x^4 = f(x)，为偶函数。\n"
     "(2) f(-x) = (-x) cos(1/(-x)) = -x cos(1/x) = -f(x)，为奇函数。\n"
     "(3) f(-x) = -x + arctan(-x) - e^(-2x+1)，既不等于 f(x) 也不等于 -f(x)，非奇非偶。\n"
     "(4) f(-x) = ln(-x + √(1+(-x)^2)) = ln(√(1+x^2)-x) = -ln(x+√(1+x^2)) = -f(x)，为奇函数。"),

    ("4. 证明：f(x) 在 X 上有界的充要条件是 f(x) 在 X 上既有下界又有上界",
     "证明：\n"
     "充分性：若 f(x) 在 X 上有界，则存在 M>0，对任意 x∈X 有 |f(x)| <= M，\n"
     "即 f(x) <= M（有上界，取 K2=M）且 f(x) >= -M（有下界，取 K1=-M）。\n"
     "必要性：若 f(x) 在 X 上既有下界 K1 又有上界 K2，取 M = max(|K1|,|K2|)，\n"
     "则对任意 x∈X 有 -M <= K1 <= f(x) <= K2 <= M，即 |f(x)| <= M，故 f(x) 在 X 上有界。\n"
     "综上，有界 等价于 既有下界又有上界。证毕。"),

    ("5. 证明：两偶函数之积为偶函数，两奇函数之积为偶函数，奇×偶为奇函数",
     "证明：设 f,g 定义域关于原点对称。\n"
     "(1) f,g 均偶：(fg)(-x)=f(-x)g(-x)=f(x)g(x)=(fg)(x)，故 fg 为偶函数。\n"
     "(2) f,g 均奇：(fg)(-x)=f(-x)g(-x)=(-f(x))(-g(x))=f(x)g(x)=(fg)(x)，故 fg 为偶函数。\n"
     "(3) f 奇、g 偶：(fg)(-x)=f(-x)g(-x)=(-f(x))g(x)=-(fg)(x)，故 fg 为奇函数。证毕。"),

    ("6. 设 f 奇、g 偶，说明 f[f(x)], g[g(x)], f[g(x)], g[f(x)] 的奇偶性",
     "(1) f[f(x)]：f(f(-x)) = f(-f(x)) = -f(f(x))，为奇函数。\n"
     "(2) g[g(x)]：g(g(-x)) = g(g(x))，为偶函数。\n"
     "(3) f[g(x)]：f(g(-x)) = f(g(x))，且 f(g(-x)) = (f(g))(-x)，故 (f(g))(x) = (f(g))(-x)，为偶函数。\n"
     "(4) g[f(x)]：g(f(-x)) = g(-f(x)) = g(f(x))，为偶函数。\n"
     "结论：奇、偶、偶、偶。"),
]


class PDF(FPDF):
    def header(self):
        self.set_font("simhei", "", 12)
        self.cell(0, 8, "高等数学 第一章 作业（学生作答）", align="L")
        self.ln(10)

    def footer(self):
        self.set_y(-12)
        self.set_font("simhei", "", 9)
        self.cell(0, 8, f"第 {self.page_no()} 页", align="C")


pdf = PDF(format="A4")
pdf.add_font("simhei", "", FONT, uni=True)
pdf.set_auto_page_break(auto=True, margin=15)

# 封面/信息页
pdf.add_page()
pdf.set_font("simhei", "", 13)
pdf.cell(0, 9, "学生信息", ln=True)
pdf.set_font("simhei", "", 11)
pdf.cell(0, 8, "姓名：王小明    学号：20231101    班级：数科2301", ln=True)
pdf.cell(0, 8, "章节：第一章 §1.1 函数与极限", ln=True)
pdf.ln(4)

for title, body in PAGES:
    pdf.add_page()
    pdf.set_font("simhei", "", 12)
    pdf.multi_cell(0, 7, title)
    pdf.ln(2)
    pdf.set_font("simhei", "", 11)
    pdf.multi_cell(0, 6.5, body)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
pdf.output(OUT)
print("WROTE", OUT, os.path.getsize(OUT), "bytes")
