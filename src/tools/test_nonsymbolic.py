# -*- coding: utf-8 -*-
"""验证非符号型比对器对真实答案的覆盖效果"""
import sys, re
sys.stdout.reconfigure(encoding="utf-8")
import grading_engine as ge

def show(label, std, student):
    eq, conf, how = ge.answer_match(student, std)
    ct = ge.classify_candidate(std)
    print(f"[{label}] type={ct}  eq={eq} conf={conf}  {how}")
    print(f"    std : {std[:80]}")
    print(f"    stud: {student[:80]}")

print("===== 区间比对 =====")
show("定义域并集", r"(-infty,-4]cup[1,+infty)", r"(-infty,-4]cup[1,+infty)")
show("单调区间", r"[-1,+infty)", r"[-1,+infty)")
show("区间转写 O->0", r"[O,+oo)", r"[0,+oo)")
show("多区间乱序", r"[1,+infty)cup(-infty,-4]", r"(-infty,-4]cup[1,+infty)")
show("区间不等(错答)", r"[-1,+infty)", r"(0,+infty)")

print("\n===== 极限比对 =====")
show("极限末值0", r"lim_{n\to\infty}(((-1)^n)/(2n+3))=0", "0")
show("极限末值0(带lim)", r"lim_{n\to\infty}(((-1)^n)/(2n+3))=0", r"lim_{n\to\infty}(((-1)^n)/(2n+3))=0")
show("极限末值pi", r"pi", "pi")
show("无极限对无极限", "无极限", "不存在")
show("无极限vs有值(错)", "无极限", "0")
show("极限末值1", r"lim_{n\to\infty}\sqrt[n]{4n^3+5}=1", "1")

print("\n===== 导数记号比对 =====")
show("f'(x0)系数", r"3f'(x_0)", r"3f'(x0)")
show("y'=表达式", r"y'=tan(x+1)", r"dy/dx=tan(x+1)")
show("n阶导数", r"4^(n-1)*cos(4x+((n)/(2))pi)", r"4^(n-1)*cos(4*x+(n/2)*pi)")
show("f''复合", r"2f'(x^2)cos[f(x^2)]", r"2*fprime(x^2)*cos(f(x^2))")

print("\n===== 回归：原有符号判等仍正常 =====")
show("2.2#2候选2", r"3^x \ln 3 \cos x - 3^x \sin x", r"3**x*log(3)*cos(x)-3**x*sin(x)")
show("简单值", "-2", "-2")
