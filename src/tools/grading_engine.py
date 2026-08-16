# -*- coding: utf-8 -*-
"""
自动批改引擎 —— 阶段三核心
设计原则：
  1. 计算题走符号计算判等（SymPy），确定性高，可全自动
  2. 证明题不自动定分，只输出 AI 参考建议 + 风险标记，交教师终审
  3. 每个判定都带置信度，低置信度优先推给教师复核
"""
from __future__ import annotations
import re, json, sys, os, base64
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

import sympy as sp
from sympy.parsing.sympy_parser import (
    parse_expr, standard_transformations,
    implicit_multiplication_application, convert_xor,
)

TRANSFORMS = standard_transformations + (
    implicit_multiplication_application, convert_xor,
)

# ============ 数据结构 ============

@dataclass
class StepRule:
    """关键步骤评分点"""
    key: str                  # 步骤标识
    patterns: List[str]       # 命中任一正则即视为出现该步骤
    weight: float             # 该步骤分值权重
    desc: str = ""            # 步骤说明（用于反馈）


@dataclass
class ProblemSpec:
    """一道题的批改配置"""
    pid: str
    ptype: str                       # calc | proof
    max_score: float
    std_answer: Optional[str] = None # 标准答案表达式（calc 用）
    answer_tol: float = 1e-6         # 数值容差
    steps: List[StepRule] = field(default_factory=list)
    answer_weight: float = 0.6       # 最终答案占比（其余给步骤）
    keywords_required: List[str] = field(default_factory=list)  # proof 用


@dataclass
class GradeResult:
    pid: str
    score: float
    max_score: float
    correct: Optional[bool]
    confidence: float
    need_review: bool
    detail: Dict[str, Any] = field(default_factory=dict)
    feedback: str = ""


# ============ 表达式归一化 ============

_CLEAN_MAP = {
    "×": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "—": "-",
    "（": "(", "）": ")", "，": ",", "。": ".", "∞": "oo",
    "π": "pi", "＋": "+", "－": "-", "＝": "=", "^": "**",
}

_FUNC_ALIAS = {
    "ln": "log",      # SymPy 自然对数用 log
    "sec": "sec",     # sympy 无 sec，后续用 1/cos 展开
    "csc": "csc",     # sympy 无 csc
    "cot": "cot",     # sympy 无 cot
}

def normalize_expr(text: str) -> str:
    """把 OCR / 手写识别出的表达式清洗成 SymPy 可解析形式"""
    if text is None:
        return ""
    s = str(text).strip()
    for k, v in _CLEAN_MAP.items():
        s = s.replace(k, v)
    s = s.replace("**", "^")            # 先统一成 ^，交给 convert_xor
    s = re.sub(r"\s+", "", s)
    # 常见 LaTeX 片段还原
    s = re.sub(r"\\d?frac\{([^{}]+)\}\{([^{}]+)\}", r"((\1)/(\2))", s)
    s = re.sub(r"\\sqrt\{([^{}]+)\}", r"sqrt(\1)", s)
    # \func{arg} 或 \func arg -> func(arg)
    FUNCS = ("arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
             "sin", "cos", "tan", "cot", "sec", "csc", "ln", "log", "exp", "sqrt")
    for f in FUNCS:
        # \func{arg} -> func(arg)
        s = re.sub(rf"\\({f})\\{{([^{{}}]+)\\}}", rf"{f}(\2)", s)
        # \func x, \func 3 -> func(x), func(3)
        s = re.sub(rf"\\({f})([a-zA-Z0-9])", rf"{f}(\2)", s)
    s = re.sub(r"\\(sin|cos|tan|cot|ln|log|exp|arcsin|arccos|arctan)", r"\1", s)
    s = s.replace("\\infty", "oo")                      # LaTeX 无穷符号
    s = s.replace("\\left", "").replace("\\right", "")   # LaTeX 定界符
    s = s.replace("\\", "")

    # 去掉指数上的花括号: ^{2} -> ^2
    s = re.sub(r'\^\{(\d+)\}', r'^\1', s)

    # 在 func^ 前补显式乘号：xsec^2x -> x*sec^2x，使后续 func^power 模式能匹配
    for f in FUNCS:
        s = re.sub(rf"([a-zA-Z0-9\)]){f}\^", rf"\1*{f}^", s)

    # --- 处理 func^power arg 模式：sec^2x -> sec(x)^2 ---
    for f in FUNCS:
        # sec^2x -> sec(x)^2  (power后跟裸变量)
        s = re.sub(rf"(?<![a-zA-Z]){f}\^(\d+)([a-zA-Z])", rf"{f}(\2)^\1", s)
        # sec^2(x) -> sec(x)^2  (power后跟带括号参数)
        s = re.sub(rf"(?<![a-zA-Z]){f}\^(\d+)\(([^)]+)\)", rf"{f}(\2)^\1", s)

    # 残余花括号转圆括号
    s = s.replace("{", "(").replace("}", ")")

    s = re.sub(r"^[a-zA-Z]*=", "", s)   # 去掉 "y=" 之类前缀

    # --- 手写/OCR 容错：函数名与自变量粘连时补括号 ---
    # cosa -> cos(a)  sinx -> sin(x)  lnx -> ln(x)  ln3 -> ln(3)
    for f in FUNCS:
        # 函数名后紧跟单个字母/数字（且非'('、非更长标识符）
        s = re.sub(rf"\b{f}([a-zA-Z0-9])(?![a-zA-Z0-9(])", rf"{f}(\1)", s)
        # 函数名后直接跟数字（多位数）的情况：ln10 -> ln(10)
        s = re.sub(rf"\b{f}(\d+)(?![a-zA-Z0-9(])", rf"{f}(\1)", s)

    # SymPy 自然对数用 log 而不是 ln
    s = s.replace("ln(", "log(")

    # 在函数调用前补显式乘号，避免 xlog(3) 被解析为 x*l*o*g(3)
    FUNCS_SYM = ("arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
                 "sin", "cos", "tan", "cot", "sec", "log", "exp", "sqrt")
    for f in FUNCS_SYM:
        s = re.sub(rf"([a-zA-Z0-9\)]){f}\(", rf"\1*{f}(", s)

    # 去掉末尾的句号/分号，避免 SymPy 解析失败
    s = re.sub(r"[;.]+$", "", s)
    return s


def to_sympy(text: str):
    s = normalize_expr(text)
    if not s:
        return None
    try:
        return parse_expr(s, transformations=TRANSFORMS, evaluate=True)
    except Exception:
        return None


def expr_equal(a: str, b: str, tol: float = 1e-6) -> tuple[bool, float, str]:
    """
    判断两个表达式是否等价
    返回 (是否相等, 置信度, 判定方式)
    """
    ea, eb = to_sympy(a), to_sympy(b)
    if ea is None or eb is None:
        # 退化为字符串比较
        na, nb = normalize_expr(a), normalize_expr(b)
        if na and na == nb:
            return True, 0.55, "字符串完全一致（表达式解析失败）"
        return False, 0.30, "表达式解析失败，无法判定"

    # 1) 符号化简判等
    try:
        diff = sp.simplify(ea - eb)
        if diff == 0:
            return True, 0.99, "符号化简判等"
    except Exception:
        pass

    # 2) 数值判等（分级容差：区分"精确" / "合理近似" / "错误"）
    try:
        va, vb = complex(sp.N(ea)), complex(sp.N(eb))
        denom = max(1.0, abs(vb))
        rel = abs(va - vb) / denom
        if rel < tol:
            return True, 0.95, "数值判等"
        # 学生手写常给小数近似（如 0.6667 ≈ 2/3），按相对误差宽容判定
        if rel < 1e-3:
            return True, 0.80, (f"数值近似判等（相对误差 {rel:.2e}，"
                                f"学生答 {va.real:.6g} vs 标准 {vb.real:.6g}）")
        return False, 0.95, f"数值不等 ({va.real:.6g} vs {vb.real:.6g})"
    except Exception:
        pass

    # 3) 随机取点判等（含自由变量的表达式）
    try:
        syms = sorted(ea.free_symbols | eb.free_symbols, key=str)
        if syms:
            import random
            hits = 0
            for _ in range(12):
                sub = {s: sp.Rational(random.randint(2, 40), random.randint(1, 7))
                       for s in syms}
                da = complex(sp.N(ea.subs(sub)))
                db = complex(sp.N(eb.subs(sub)))
                if abs(da - db) < 1e-6 * max(1.0, abs(da)):
                    hits += 1
            if hits >= 11:
                return True, 0.90, f"随机取点判等 ({hits}/12)"
            return False, 0.85, f"随机取点不等 ({hits}/12)"
    except Exception:
        pass

    return False, 0.40, "无法可靠判定"


# ============ 非符号型专用比对器 ============
#
# 很多高数答案并不是"一个可符号化简的表达式"，而是：
#   - 区间（定义域/单调区间/收敛域）：(-∞,−4]∪[1,+∞)
#   - 极限（只关心末值或"不存在"）：lim_{n→∞}((−1)^n/(2n+3))=0 / 无极限
#   - 含导数记号的表达式：3f'(x₀) / y'=tan(x+1) / 2f'(x²)cos[...]
# 这些用纯 SymPy 判等会直接解析失败、一律转人工。下面三类比对器把它们也变成可自动判分。

def _find_interval_pieces(s: str):
    """返回 s 中所有"括号配平"的片段，支持区间里嵌套分数括号，
       且兼容半开区间 [a,b) / (a,b]（开/闭括号可不同类型）。
       如 (-infty,((5)/(2))) 与 [-1,3) 都会被完整识别。"""
    pieces, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "([":
            depth, j = 1, i + 1
            while j < n:
                if s[j] in "([":
                    depth += 1
                elif s[j] in ")]":
                    depth -= 1
                    if depth == 0:
                        pieces.append(s[i:j + 1])
                        i = j + 1
                        break
                j += 1
            else:
                i += 1
                continue
        else:
            i += 1
    return pieces


def _looks_interval(s: str) -> bool:
    """判断文本是否含有区间记号（一个括号配平片段，中间逗号分隔，
       且至少一个界含数字或无穷）"""
    for p in _find_interval_pieces(s):
        inner = p[1:-1]
        depth = 0
        for k, ch in enumerate(inner):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                lo, hi = inner[:k].strip(), inner[k + 1:].strip()
                if re.search(r"infty|oo|\d", lo) or re.search(r"infty|oo|\d", hi):
                    return True
                break
    return False


def classify_candidate(cand: str) -> str:
    """判断候选答案类型，决定走哪个比对器：
       interval | limit | deriv | symbolic | empty
    """
    s = (cand or "").strip()
    if not s:
        return "empty"
    # 区间优先（且不是 lim_ 表达式）
    if _looks_interval(s) and not re.search(r"lim_", s):
        return "interval"
    # 极限：含 lim_ 或"无极限/不存在/发散"等结论词
    if re.search(r"lim_", s) or re.search(r"无极限|不存在|发散|没有极限", s):
        return "limit"
    # 含导数记号 f'(..) / g'' / y' 等
    if re.search(r"[a-zA-Z]''?\(", s) or re.search(r"\by'", s) \
            or re.search(r"f'|g'|h'|y'", s):
        return "deriv"
    return "symbolic"


# ---------- 区间比对 ----------

def _interval_bound(tok: str):
    tok = tok.strip()
    if tok in ("-infty", "-inf", "-oo"):
        return "-inf"
    if tok in ("+infty", "+inf", "inf", "oo", "+oo"):
        return "+inf"
    v = to_sympy(tok)
    if v is None:
        return None
    try:
        return float(sp.N(v))
    except Exception:
        return None


def _parse_interval_piece(piece: str):
    """解析单个区间片段（支持嵌套括号），返回 (lo, lb_closed, hi, ub_closed)"""
    piece = piece.strip()
    if len(piece) < 3:
        return None
    lb, rb = piece[0], piece[-1]
    if lb not in "([" or rb not in ")]":
        return None
    inner = piece[1:-1]
    depth = 0
    for k, ch in enumerate(inner):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            lo = _interval_bound(inner[:k].strip())
            hi = _interval_bound(inner[k + 1:].strip())
            if lo is None or hi is None:
                return None
            return (lo, lb == "[", hi, rb == "]")
    return None


def _piece_key(p):
    lo, lb, hi, ub = p
    lon = -1e300 if lo == "-inf" else lo
    hin = 1e300 if hi == "+inf" else hi
    if lo == "-inf":
        lb = False
    if hi == "+inf":
        ub = False
    return (round(lon, 9), lb, round(hin, 9), ub)


def compare_interval(std: str, ans: str, tol: float = 1e-6):
    """两个区间集合（支持 ∪/cup 拼接）作为无序集合判等"""
    def pieces(s):
        s = normalize_expr(s)                     # \cup -> cup, ∞ -> oo 等
        out = []
        for piece in re.split(r"\\?cup", s):      # 同时兼容 \cup 与 cup
            p = _parse_interval_piece(piece)
            if p:
                out.append(_piece_key(p))
        return out
    sp_set = pieces(std)
    ap_set = pieces(ans)
    if not sp_set or not ap_set:
        return False, 0.30, "区间解析失败"
    if sorted(sp_set) == sorted(ap_set):
        return True, 0.92, "区间集合判等"
    return False, 0.90, "区间集合不等"


# ---------- 极限比对 ----------

_NO_LIMIT = re.compile(r"无极限|不存在|发散|没有极限")


def _extract_limit_value(expr: str):
    """从 lim...=值 或 值 中提取最终值；返回 (value_str|None, is_no_limit)"""
    s = (expr or "").strip()
    if _NO_LIMIT.search(s):
        return (None, True)
    if "=" in s:
        val = s.rsplit("=", 1)[1].strip()
    else:
        val = s
    val = re.sub(r"lim_.*", "", val).strip()   # 去除残留 lim_ 片段
    if not val:
        return (None, False)
    return (val, False)


def compare_limit(std: str, ans: str, tol: float = 1e-6):
    sv, sno = _extract_limit_value(std)
    av, ano = _extract_limit_value(ans)
    # 双方都是"无极限"类结论
    if sno and ano:
        return True, 0.85, "均无极限/发散"
    if sno != ano:
        return False, 0.85, "极限存在性结论不一致"
    if sv is None or av is None:
        return expr_equal(ans, std, tol)      # 退化到符号比较
    return expr_equal(av, sv, tol)


# ---------- 导数记号表达式比对 ----------
#
# 注意：不能把 f'(x) 转成 fprime(x) 再交给 SymPy——SymPy 会把多字母名
# fprime 拆成 f*p*r*i*m*e 六个字母相乘，彻底失真。导数式（尤其是
# "系数 × f'(...)" 这类）本就是同形比较，所以走【结构字符串比对】：
# 归一化 + 统一导数记号/下标 + 取方程右端，再做字符串一致判定。

def _canon_deriv(text: str):
    s = normalize_expr(text)
    s = s.replace("[", "(").replace("]", ")")          # 方括号当圆括号
    # 函数调用前补显式 *（含 ]->) 后的 cos[ 等情形，幂等）
    for f in ("sin", "cos", "tan", "cot", "sec", "csc", "ln", "log",
              "exp", "sqrt", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh"):
        s = re.sub(rf"([a-zA-Z0-9\)])\*?{f}\(", rf"\1*{f}(", s)
    # 在数字/字母/括号后补 * 再接 f'/f''/g'/y' 等导数记号
    s = re.sub(r"([a-zA-Z0-9\)])\*?([fgh]''?\()", r"\1*\2", s)
    s = re.sub(r"([a-zA-Z0-9\)])\*?y'", r"\1*y'", s)
    s = re.sub(r"_(\d+)", r"\1", s)                    # x_0 -> x0 统一下标
    if "=" in s:                                       # 取方程右端比较
        s = s.rsplit("=", 1)[1].strip()
    return s


def compare_deriv(std: str, ans: str, tol: float = 1e-6):
    cs, ca = _canon_deriv(std), _canon_deriv(ans)
    if cs and cs == ca:
        return True, 0.92, "导数式结构一致"
    if not cs or not ca:
        return False, 0.50, "导数表达式解析失败"
    return expr_equal(ans, std, tol)                   # 退化符号比较


# ---------- 总调度 ----------

def answer_match(student: str, cand: str, tol: float = 1e-6):
    """根据标准答案候选的类型选择比对器；解析失败退化到字符串比较"""
    ctype = classify_candidate(cand)
    if ctype == "interval":
        return compare_interval(cand, student, tol)
    if ctype == "limit":
        return compare_limit(cand, student, tol)
    if ctype == "deriv":
        return compare_deriv(cand, student, tol)
    return expr_equal(student, cand, tol)


# ============ 步骤匹配 ============

def match_steps(student_work: str, rules: List[StepRule]) -> Dict[str, bool]:
    text = student_work or ""
    flat = re.sub(r"\s+", "", text)
    hit = {}
    for r in rules:
        ok = False
        for p in r.patterns:
            pp = re.sub(r"\s+", "", p)
            try:
                if re.search(pp, flat):
                    ok = True
                    break
            except re.error:
                if pp in flat:
                    ok = True
                    break
        hit[r.key] = ok
    return hit


# ============ 批改主逻辑 ============

def grade_calc(spec: ProblemSpec, final_answer: str, work: str = "") -> GradeResult:
    # 支持一个标准答案里用 " ||| " 分隔多个候选答案（适用于大题含多子题的情况）
    std = spec.std_answer or ""
    candidates = [c.strip() for c in re.split(r'\s*\|\|\|\s*', std) if c.strip()]
    if not candidates:
        candidates = [std]

    best_eq = False
    best_conf = 0.0
    best_how = "无标准答案"
    for cand in candidates:
        eq, conf, how = answer_match(final_answer, cand, spec.answer_tol)
        if eq:
            best_eq = True
            best_conf = max(best_conf, conf)
            best_how = how
            break
        if conf > best_conf:
            best_conf = conf
            best_how = how
    eq, conf, how = best_eq, best_conf, best_how

    ans_full = spec.max_score * spec.answer_weight
    step_full = spec.max_score - ans_full

    ans_score = ans_full if eq else 0.0

    step_hits = match_steps(work, spec.steps)
    if spec.steps:
        wsum = sum(r.weight for r in spec.steps) or 1.0
        got = sum(r.weight for r in spec.steps if step_hits.get(r.key))
        step_score = step_full * got / wsum
    else:
        # 无步骤规则时，步骤分随答案给
        step_score = step_full if eq else 0.0

    # 答案错但步骤对 → 过程分保留，提示计算失误
    total = round(ans_score + step_score, 1)

    missing = [r.desc or r.key for r in spec.steps if not step_hits.get(r.key)]
    if eq and not missing:
        fb = "答案正确，步骤完整。"
    elif eq and missing:
        fb = "答案正确，但未体现关键步骤：" + "、".join(missing)
    elif not eq and not missing:
        fb = f"步骤方向正确但最终结果有误（{how}）。请检查计算过程。"
    else:
        fb = f"结果有误（{how}）；缺少关键步骤：" + "、".join(missing)

    need_review = (conf < 0.85) or (not eq and step_score > 0)
    return GradeResult(
        pid=spec.pid, score=total, max_score=spec.max_score,
        correct=eq, confidence=round(conf, 2), need_review=need_review,
        detail={"answer_score": round(ans_score, 1),
                "step_score": round(step_score, 1),
                "step_hits": step_hits, "method": how},
        feedback=fb,
    )


def grade_proof(spec: ProblemSpec, work: str) -> GradeResult:
    """
    证明题：不自动定分，只给参考建议
    输出 suggested_score 供教师参考，need_review 恒为 True
    """
    hits = match_steps(work, spec.steps)
    wsum = sum(r.weight for r in spec.steps) or 1.0
    got = sum(r.weight for r in spec.steps if hits.get(r.key))
    coverage = got / wsum

    kw_hit = [k for k in spec.keywords_required
              if re.sub(r"\s+", "", k) in re.sub(r"\s+", "", work or "")]
    kw_rate = len(kw_hit) / len(spec.keywords_required) if spec.keywords_required else 1.0

    suggested = round(spec.max_score * (0.7 * coverage + 0.3 * kw_rate), 1)
    missing = [r.desc or r.key for r in spec.steps if not hits.get(r.key)]

    if coverage >= 0.85:
        fb = "论证结构较完整，建议重点核对逻辑严密性。"
    elif coverage >= 0.5:
        fb = "论证覆盖部分要点，缺少：" + "、".join(missing)
    else:
        fb = "论证要点覆盖不足，缺少：" + "、".join(missing) + "。建议人工细看。"

    return GradeResult(
        pid=spec.pid, score=suggested, max_score=spec.max_score,
        correct=None,                 # 证明题不下结论
        confidence=round(min(0.75, 0.35 + 0.4 * coverage), 2),
        need_review=True,             # 恒需人工
        detail={"coverage": round(coverage, 2), "keyword_rate": round(kw_rate, 2),
                "step_hits": hits, "mode": "AI建议分（非最终分）"},
        feedback=fb,
    )


def grade(spec: ProblemSpec, final_answer: str = "", work: str = "") -> GradeResult:
    if spec.ptype == "calc":
        return grade_calc(spec, final_answer, work)
    return grade_proof(spec, work)


# ============ 手写 OCR 管线 ============

_OCR_ENGINE = None

def _get_ocr():
    """延迟初始化 RapidOCR（首次调用时加载模型）"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
        except ImportError:
            return None
    return _OCR_ENGINE


def ocr_handwritten(image_path: str) -> str:
    """对手写作答图片执行 OCR，返回识别文本
    
    支持：
    - 本地图片路径（.png/.jpg/.jpeg/.bmp）
    - Base64 编码的图片数据
    - 如果 RapidOCR 不可用，返回空字符串
    """
    engine = _get_ocr()
    if engine is None:
        return ""

    # 处理 base64
    img_data = image_path
    if isinstance(image_path, str) and image_path.startswith("data:"):
        try:
            # data:image/png;base64,xxxx
            img_data = base64.b64decode(image_path.split(",", 1)[1])
        except Exception:
            return ""

    # 处理本地文件路径
    if isinstance(img_data, str):
        if not os.path.exists(img_data):
            return ""
        # RapidOCR 接受路径或 numpy array

    try:
        result, _ = engine(img_data)
        if not result:
            return ""
        # 拼接识别文本（按行输出）
        lines = []
        for item in result:
            text = item[1]  # (box, text, confidence)
            if text:
                lines.append(text)
        return "\n".join(lines)
    except Exception:
        return ""


def ocr_and_purify(image_path: str) -> str:
    """OCR + 数学表达式清洗：识别后自动归一化"""
    raw = ocr_handwritten(image_path)
    if not raw:
        return ""
    # 合并多行为单行表达式
    cleaned = re.sub(r"\s+", "", raw)
    return normalize_expr(cleaned)


# ============ 自测：用习题1.3的真实题目 ============

def _demo():
    sys.stdout.reconfigure(encoding="utf-8")

    specs = {
        # 6(1) lim_{x->1} (3x-5)/(2x-1) = -2
        "6-1": ProblemSpec(
            pid="6-1", ptype="calc", max_score=3, std_answer="-2",
            answer_weight=0.6,
            steps=[
                StepRule("substitute", [r"代入", r"x=1", r"x\s*→\s*1"], 1.0,
                         "直接代入 x=1（分母不为零）"),
            ],
        ),
        # 6(4) lim (sqrt(5x+4)-3)/(sqrt(3x+1)-2) at x->1  = 2/3 * ... 演示有理化
        "6-4": ProblemSpec(
            pid="6-4", ptype="calc", max_score=3, std_answer="2/3",
            steps=[
                StepRule("rationalize", [r"有理化", r"分子.*分母.*同乘", r"共轭"], 1.5,
                         "分子分母有理化"),
                StepRule("simplify", [r"约分", r"化简"], 1.0, "约去零因子"),
            ],
        ),
        # 6(7) lim_{x->a} (sin x - sin a)/(x - a) = cos(a)
        "6-7": ProblemSpec(
            pid="6-7", ptype="calc", max_score=3, std_answer="cos(a)",
            steps=[
                StepRule("sum2prod", [r"和差化积", r"2cos.*sin"], 1.5, "和差化积"),
                StepRule("limit1", [r"重要极限", r"sin.*/.*→1", r"lim.*sin"], 1.0,
                         "使用第一个重要极限"),
            ],
        ),
        # 第1题：证明题
        "1": ProblemSpec(
            pid="1", ptype="proof", max_score=10,
            steps=[
                StepRule("def_eps", [r"任[给意].*ε", r"∀.*ε", r"epsilon"], 2.0,
                         "由极限定义引入 ε"),
                StepRule("choose_eps", [r"取.*ε\s*=", r"令.*ε\s*="], 2.0,
                         "取特定 ε（如 |a|/2）"),
                StepRule("exist_N", [r"存在.*N", r"∃.*N", r"当.*n\s*>\s*N"], 2.0,
                         "给出 N 的存在性"),
                StepRule("triangle", [r"三角不等式", r"\|.*\|\s*[≥>].*\|.*\|\s*-"], 1.5,
                         "使用三角不等式放缩"),
                StepRule("conclude", [r"故|因此|从而|得证|证毕"], 1.0, "给出结论"),
            ],
            keywords_required=["ε", "N"],
        ),
    }

    cases = [
        ("6-1", "-2", "直接代入 x=1，分母 2*1-1=1≠0，得 (3-5)/1 = -2", "标准正确解"),
        ("6-1", "-2", "答案是 -2", "答案对但无步骤"),
        ("6-1", "2", "代入 x=1 得 (3-5)/(2-1)=2", "符号错误"),
        ("6-4", "2/3", "分子分母同乘共轭因子有理化，约分后化简得 2/3", "有理化正确"),
        ("6-4", "0.6667", "有理化后约分", "数值近似答案"),
        ("6-7", "cos(a)", "用和差化积 2cos((x+a)/2)sin((x-a)/2)，再用第一个重要极限 lim sin t/t=1", "标准解"),
        ("6-7", "cosa", "和差化积后取极限", "OCR 少了括号"),
        ("1", "", "任给 ε>0，取 ε=|a|/2，由极限定义存在 N，当 n>N 时 |a_n - a|<|a|/2，"
                  "由三角不等式 |a_n| ≥ |a| - |a_n - a| > |a|/2，故得证。", "完整证明"),
        ("1", "", "因为极限是 a，所以 n 大的时候 a_n 接近 a，所以成立。", "空洞论证"),
    ]

    print("=" * 78)
    print("自动批改引擎 —— 习题1.3 真实题目验证")
    print("=" * 78)
    for pid, ans, work, note in cases:
        spec = specs[pid]
        r = grade(spec, ans, work)
        tag = "计算题" if spec.ptype == "calc" else "证明题"
        print(f"\n[{tag}] 第 {pid} 题  —— {note}")
        print(f"   学生答案: {ans or '(无)'}")
        print(f"   得分: {r.score}/{r.max_score}   "
              f"判定: {'正确' if r.correct else ('错误' if r.correct is False else 'AI建议分')}   "
              f"置信度: {r.confidence}")
        print(f"   需人工复核: {'是' if r.need_review else '否'}")
        print(f"   反馈: {r.feedback}")
        if spec.ptype == "calc":
            print(f"   明细: 答案分 {r.detail['answer_score']} + 步骤分 {r.detail['step_score']}"
                  f"  ({r.detail['method']})")
        else:
            print(f"   要点覆盖率: {r.detail['coverage']*100:.0f}%  "
                  f"关键词命中: {r.detail['keyword_rate']*100:.0f}%")

    print("\n" + "=" * 78)
    print("结论：计算题可自动定分；证明题仅给建议分并强制人工复核。")


if __name__ == "__main__":
    _demo()
