# -*- coding: utf-8 -*-
"""测试 normalize_expr 对 sec^2x 等三角函数幂次的处理"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from grading_engine import normalize_expr, to_sympy, expr_equal

cases = [
    # (input, description)
    (r"\frac{2}{1+x^2} - 3 \sec^2 x", "2.2#2 候选1: 含 sec^2x"),
    ("3*sec(x)^2", "学生答案: sec(x)^2 标准写法"),
    (r"\sec^2 x", "裸 sec^2x"),
    (r"\sec^{2}x", "花括号 sec^{2}x"),
    (r"\sec^2(x)", "带括号 sec^2(x)"),
    (r"3\sec^2x\cos x", "复合 3sec^2x cosx"),
    (r"\tan^2 x + 1", "tan^2x + 1"),
    (r"\sin^2x + \cos^2x", "sin^2x + cos^2x"),
    (r"-\frac{1}{x^2}", "不含三角函数的对照"),
]

print("=" * 70)
print("normalize_expr 测试")
print("=" * 70)
for expr, desc in cases:
    norm = normalize_expr(expr)
    sym = to_sympy(expr)
    print(f"\n[{desc}]")
    print(f"  输入: {expr}")
    print(f"  归一: {norm}")
    print(f"  SymPy: {sym}")

# 关键验证：std_answer 候选 vs 学生答案
print("\n" + "=" * 70)
print("expr_equal 验证")
print("=" * 70)

pairs = [
    (r"3*sec(x)^2", r"\frac{2}{1+x^2} - 3 \sec^2 x", "学生 vs 标准(候选1)"),
    ("2/(1+x**2)-3*sec(x)**2", r"\frac{2}{1+x^2} - 3 \sec^2 x", "Python写法 vs LaTeX"),
]

for a, b, desc in pairs:
    eq, conf, how = expr_equal(a, b)
    print(f"\n[{desc}]")
    print(f"  A: {a}")
    print(f"  B: {b}")
    print(f"  等价: {eq}  置信度: {conf}  方式: {how}")
